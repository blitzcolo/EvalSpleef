"""Provider-level tests: capabilities, spec validation, slots, labels and lifecycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_tart.provider import TartProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.sandbox import Resources, SandboxSpec, TemplateRef

SpecFactory = Callable[..., SandboxSpec]
ProviderFactory = Callable[..., TartProvider]


def _entries(state_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state_dir / "sandboxes").glob("*.json"))
    ]


def _vm_meta(fake_root: Path, index: int = 0) -> dict[str, Any]:
    vms = sorted((fake_root / "vms").iterdir())
    return json.loads((vms[index] / "meta.json").read_text(encoding="utf-8"))


async def test_capabilities_are_exact(provider: TartProvider) -> None:
    caps = provider.capabilities()
    assert caps.os_families == frozenset({OSFamily.MACOS, OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.ARM64})
    assert caps.isolation == Isolation.L3
    assert caps.net_modes == frozenset(
        {NetMode.OFF, NetMode.FULL, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
    )
    assert caps.allowlist_kinds == frozenset({"cidr"})
    assert caps.dynamic_network_policy is False
    assert caps.streaming is True
    assert caps.stdin is True
    assert caps.snapshot == "none"
    assert caps.snapshot_fanout is False
    assert caps.pause_resume is False
    assert caps.gui == "vnc"
    assert caps.gpu is False
    assert caps.port_expose is False
    assert caps.host_mounts is False
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.max_concurrency == 2
    assert caps.max_lifetime_s == 14400


async def test_host_macos_slots_config_changes_max_concurrency(
    provider_factory: ProviderFactory,
) -> None:
    provider = provider_factory(host_macos_slots=3)
    assert provider.capabilities().max_concurrency == 3


async def test_create_clones_by_digest_and_configures_vm(
    provider: TartProvider,
    spec_factory: SpecFactory,
    fake_tart_root: Path,
    state_dir: Path,
) -> None:
    spec = spec_factory("clone-1", network=NetMode.FULL)
    sandbox = await provider.create(spec)
    try:
        meta = _vm_meta(fake_tart_root)
        assert meta["source"] == "ghcr.io/evalspleef/macos-golden@sha256:" + "a" * 64
        assert meta["cpu"] == 2
        assert meta["memory"] == 4096
        assert meta["running"] is True
        assert meta["network"] == {"allow": [], "block": []}
        entries = _entries(state_dir)
        assert [entry["id"] for entry in entries] == [sandbox.id]
        assert entries[0]["labels"]["batch"] == "adapter-test"
        assert entries[0]["labels"]["spleef.provider"] == "test-tart"
        assert entries[0]["expires_at"] - entries[0]["created_at"] == pytest.approx(600, abs=2)
        assert entries[0]["os"] == "macos"
    finally:
        await sandbox.destroy()
    assert not any((fake_tart_root / "vms").iterdir())


@pytest.mark.parametrize(
    ("network", "allow_hosts", "allow_cidrs", "via_proxy", "expected"),
    [
        (NetMode.OFF, (), (), None, {"allow": [], "block": ["0.0.0.0/0"]}),
        (NetMode.FULL, (), (), None, {"allow": [], "block": []}),
        (
            NetMode.DOCS_WHITELIST,
            (),
            ("203.0.113.0/24", "198.51.100.7/32"),
            None,
            {"allow": ["203.0.113.0/24", "198.51.100.7/32"], "block": []},
        ),
        (
            NetMode.PACKAGE_MIRROR,
            ("pypi.org",),
            (),
            "203.0.113.9:3128",
            {"allow": ["203.0.113.9/32"], "block": []},
        ),
    ],
)
async def test_network_policy_maps_to_softnet_options(
    provider: TartProvider,
    spec_factory: SpecFactory,
    fake_tart_root: Path,
    network: NetMode,
    allow_hosts: tuple[str, ...],
    allow_cidrs: tuple[str, ...],
    via_proxy: str | None,
    expected: dict[str, list[str]],
) -> None:
    spec = spec_factory(
        "net",
        network=network,
        allow_hosts=allow_hosts,
        allow_cidrs=allow_cidrs,
        via_proxy=via_proxy,
    )
    sandbox = await provider.create(spec)
    try:
        assert _vm_meta(fake_tart_root)["network"] == expected
    finally:
        await sandbox.destroy()


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"os": OSFamily.WINDOWS}, CapabilityNotSupported),
        ({"arch": Arch.X86_64}, CapabilityNotSupported),
        ({"network": NetMode.OFF, "allow_cidrs": ("10.0.0.0/8",)}, PolicyNotEnforceable),
        ({"network": NetMode.DOCS_WHITELIST, "allow_hosts": ("pypi.org",)}, PolicyNotEnforceable),
        ({"network": NetMode.DOCS_WHITELIST}, PolicyNotEnforceable),
        (
            {"network": NetMode.DOCS_WHITELIST, "via_proxy": "proxy.invalid:3128"},
            PolicyNotEnforceable,
        ),
        (
            {
                "network": NetMode.PACKAGE_MIRROR,
                "via_proxy": "203.0.113.9:3128",
                "allow_cidrs": ("10.0.0.0/8",),
            },
            PolicyNotEnforceable,
        ),
        ({"network": NetMode.FULL, "allow_cidrs": ("10.0.0.0/8",)}, PolicyNotEnforceable),
        ({"max_lifetime_s": 10**9}, CapabilityNotSupported),
        ({"env": {"TASK_ID": "leak"}}, CapabilityNotSupported),
    ],
)
async def test_create_rejects_unsupported_specs(
    provider: TartProvider,
    spec_factory: SpecFactory,
    overrides: dict[str, Any],
    error: type[Exception],
) -> None:
    spec = spec_factory("reject", **overrides)
    with pytest.raises(error):
        await provider.create(spec)


async def test_create_rejects_gpu_and_non_tart_template(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    spec = spec_factory("reject-gpu")
    gpu_spec = replace(spec, resources=Resources(cpus=2, memory_mb=4096, disk_mb=20480, gpus=1))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(gpu_spec)
    oci_spec = replace(spec, template=TemplateRef(kind="oci", ref="x", digest="sha256:" + "b" * 64))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(oci_spec)


async def test_macos_slot_limit_and_linux_exemption(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    mac = [await provider.create(spec_factory(f"mac-{index}")) for index in range(2)]
    try:
        with pytest.raises(QuotaExceeded):
            await provider.create(spec_factory("mac-overflow"))
        for index in range(3):
            linux = await provider.create(spec_factory(f"linux-{index}", os=OSFamily.LINUX))
            await linux.destroy()
    finally:
        for sandbox in mac:
            await sandbox.destroy()


async def test_labels_list_and_reap(provider: TartProvider, spec_factory: SpecFactory) -> None:
    first = await provider.create(spec_factory("reap-a"))
    second = await provider.create(spec_factory("reap-b", os=OSFamily.LINUX))
    try:
        listed = await provider.list({"batch": "adapter-test"})
        assert {entry["id"] for entry in listed} == {first.id, second.id}
        only_b = await provider.list({"trial": "reap-b"})
        assert [entry["id"] for entry in only_b] == [second.id]
        assert await provider.reap({"trial": "reap-a"}, older_than_s=0) == 1
        remaining = await provider.list({"batch": "adapter-test"})
        assert [entry["id"] for entry in remaining] == [second.id]
        with pytest.raises(SandboxLost):
            await first.exec(["true"])
    finally:
        await second.destroy()


async def test_preflight_reports_host_facts(provider: TartProvider) -> None:
    report = await provider.preflight()
    assert report["provider"] == "tart/test-tart"
    assert report["apple_silicon"] is True
    assert report["host_arch"] == "arm64"
    assert report["tart_version"].startswith("tart ")
    assert report["host_macos_slots"] == 2


async def test_injected_create_failure_then_retry(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    provider.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await provider.create(spec_factory("fail-1"))
    retried = await provider.create(spec_factory("fail-1-retry"))
    try:
        result = await retried.exec(["sh", "-c", "exit 0"])
        assert result.exit_code == 0
    finally:
        await retried.destroy()


async def test_lose_and_expire_remove_live_vms(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    sandbox = await provider.create(spec_factory("lose-1"))
    await provider.lose(sandbox.id)
    assert provider.is_live(sandbox.id) is False
    assert await provider.list({"trial": "lose-1"}) == []
    other = await provider.create(spec_factory("expire-1"))
    await provider.expire_live_sandboxes()
    assert provider.is_live(other.id) is False
