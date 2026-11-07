"""Provider-level tests: capabilities, domain lifecycle, labels, faults, validation."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest
from evalspleef_sbx_libvirt._fake import FakeLibvirtHost
from evalspleef_sbx_libvirt.provider import LibvirtProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.sandbox import NetworkPolicy


def test_capabilities(provider: LibvirtProvider) -> None:
    caps = provider.capabilities()
    assert caps.isolation is Isolation.L3
    assert caps.os_families == frozenset({OSFamily.LINUX, OSFamily.WINDOWS})
    assert caps.arches == frozenset({Arch.X86_64})
    assert caps.net_modes == frozenset({NetMode.OFF, NetMode.FULL})
    assert caps.allowlist_kinds == frozenset()
    assert caps.gui == "vnc"
    assert caps.streaming and caps.stdin
    assert caps.snapshot == "none"
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.max_concurrency == 4


async def test_create_defines_domain_and_overlay(
    provider: LibvirtProvider, fake_host: FakeLibvirtHost
) -> None:
    spec = fake_host.spec_factory("domain-1", network=NetMode.OFF, max_lifetime_s=600)
    sandbox = await provider.create(spec)
    try:
        assert sandbox.overlay_path is not None
        assert sandbox.overlay_path.exists()
        content = sandbox.overlay_path.read_text(encoding="utf-8")
        assert "backing=" in content
        assert str(fake_host.golden) in content

        entries = await provider.list({"trial": "domain-1"})
        assert [entry["id"] for entry in entries] == [sandbox.id]
        labels = entries[0]["labels"]
        assert labels["lease"] == "lease-domain-1"
        assert labels["spleef.provider"] == "libvirt-fake"
        assert labels["spleef.template_digest"] == spec.template.digest
        assert int(labels["spleef.expires_at"]) > int(labels["spleef.created_at"])

        state = json.loads(fake_host.state_file.read_text(encoding="utf-8"))
        xml = state["domains"][sandbox.id]["xml"]
        assert "<vcpu" in xml and "MiB" in xml
        assert 'network="spleef-off"' in xml
    finally:
        await sandbox.destroy()
    assert not sandbox.overlay_path.exists()
    assert await provider.list({"trial": "domain-1"}) == []


async def test_destroy_is_idempotent(provider: LibvirtProvider, sandbox) -> None:
    await sandbox.destroy()
    await sandbox.destroy()
    assert await provider.list({"trial": "unit-1"}) == []


async def test_reap_by_labels_and_ttl(
    provider: LibvirtProvider, fake_host: FakeLibvirtHost
) -> None:
    sandbox = await provider.create(
        fake_host.spec_factory("reap-1", network=NetMode.OFF, max_lifetime_s=600)
    )
    assert await provider.reap({"trial": "reap-1"}, older_than_s=0) == 1
    assert await provider.list({"trial": "reap-1"}) == []
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_lose_marks_sandbox_lost(
    provider: LibvirtProvider, fake_host: FakeLibvirtHost
) -> None:
    sandbox = await provider.create(
        fake_host.spec_factory("lost-1", network=NetMode.OFF, max_lifetime_s=600)
    )
    await provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()


async def test_fail_next_creates(provider: LibvirtProvider, fake_host: FakeLibvirtHost) -> None:
    provider.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await provider.create(
            fake_host.spec_factory("fail-1", network=NetMode.OFF, max_lifetime_s=600)
        )
    sandbox = await provider.create(
        fake_host.spec_factory("fail-2", network=NetMode.OFF, max_lifetime_s=600)
    )
    result = await sandbox.exec(["true"])
    assert result.exit_code == 0
    await sandbox.destroy()


async def test_preflight(provider: LibvirtProvider) -> None:
    info = await provider.preflight()
    assert info["provider"] == "libvirt/libvirt-fake"
    assert "libvirt" in info["virsh_version"]
    assert info["guest_os"] == ["linux", "windows"]
    assert info["arches"] == ["x86_64"]


async def test_fake_binaries_come_from_temp_path(
    fake_host: FakeLibvirtHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert shutil.which("virsh") == str(fake_host.bin_dir / "virsh")
    assert shutil.which("qemu-img") == str(fake_host.bin_dir / "qemu-img")
    instance = fake_host.provider(use_path=True)
    try:
        sandbox = await instance.create(
            fake_host.spec_factory("path-1", network=NetMode.OFF, max_lifetime_s=600)
        )
        assert sandbox.overlay_path is not None and sandbox.overlay_path.exists()
        await sandbox.destroy()
    finally:
        await instance.close()


async def test_guest_agent_fallback_when_ssh_unreachable(
    fake_host: FakeLibvirtHost,
) -> None:
    instance = fake_host.provider(
        config={
            "wait_for_ssh": False,
            "ssh_host": "127.0.0.1",
            "ssh_port": 1,
            "ssh_connect_timeout_s": 0.2,
            "guest_agent_fallback": True,
        }
    )
    try:
        sandbox = await instance.create(
            fake_host.spec_factory("agent-1", network=NetMode.OFF, max_lifetime_s=600)
        )
        result = await sandbox.exec(["true"], timeout_s=5.0)
        assert result.exit_code == 0
        assert result.stdout == b"guest-agent-fallback"
        assert not result.timed_out
        await sandbox.destroy()
    finally:
        await instance.close()


async def test_validation_rejects_unsupported_specs(
    provider: LibvirtProvider, fake_host: FakeLibvirtHost
) -> None:
    base = fake_host.spec_factory("validate", network=NetMode.OFF, max_lifetime_s=600)
    cases = [
        (replace(base, os=OSFamily.MACOS), CapabilityNotSupported),
        (replace(base, arch=Arch.ARM64), CapabilityNotSupported),
        (
            replace(base, template=replace(base.template, kind="oci")),
            CapabilityNotSupported,
        ),
        (
            replace(base, resources=replace(base.resources, gpus=1)),
            CapabilityNotSupported,
        ),
        (replace(base, max_lifetime_s=999_999), PolicyNotEnforceable),
        (
            replace(base, network=NetworkPolicy(mode=NetMode.DOCS_WHITELIST)),
            PolicyNotEnforceable,
        ),
        (
            replace(base, network=NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",))),
            PolicyNotEnforceable,
        ),
        (
            replace(base, network=NetworkPolicy(mode=NetMode.OFF, allow_hosts=("example.com",))),
            PolicyNotEnforceable,
        ),
    ]
    for spec, error in cases:
        with pytest.raises(error):
            await provider.create(spec)


async def test_gui_none_rejects_gui_sandbox(fake_host: FakeLibvirtHost) -> None:
    instance = fake_host.provider(config={"gui": "none"})
    try:
        assert instance.capabilities().gui == "none"
        spec = fake_host.spec_factory("gui-1", network=NetMode.OFF, max_lifetime_s=600)
        with pytest.raises(CapabilityNotSupported):
            await instance.create(replace(spec, gui=True))
    finally:
        await instance.close()
