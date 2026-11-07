"""Provider-level tests against the fake ``wsb`` executable (not a real host)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from evalspleef_sbx_windows_sandbox.provider import WindowsSandboxProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
)
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


async def test_dev_only_marker_and_capabilities(provider: WindowsSandboxProvider) -> None:
    caps = provider.capabilities()
    assert provider.dev_only is True
    assert caps.os_families == frozenset({OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.X86_64})
    assert caps.isolation == Isolation.L3
    assert caps.net_modes == frozenset({NetMode.OFF, NetMode.FULL})
    assert caps.allowlist_kinds == frozenset()
    assert caps.dynamic_network_policy is False
    assert caps.streaming is False
    assert caps.stdin is False
    assert caps.snapshot == "none"
    assert not caps.snapshot_fanout and not caps.pause_resume
    assert caps.gui == "none" and not caps.gpu and not caps.port_expose
    assert not caps.host_mounts
    assert caps.enforces_limits == frozenset()
    assert caps.max_concurrency == 1
    assert caps.max_lifetime_s == 1800


async def test_create_labels_and_single_instance(
    provider: WindowsSandboxProvider, spec_factory
) -> None:
    spec = spec_factory("provider-1")
    sandbox = await provider.create(spec)
    assert sandbox.id.startswith("wsb-")
    assert sandbox.spec.labels["trial"] == "provider-1"
    entries = await provider.list(dict(spec.labels))
    assert [entry["id"] for entry in entries] == [sandbox.id]
    assert entries[0]["labels"]["spleef.provider"] == provider.instance

    with pytest.raises(QuotaExceeded):
        await provider.create(spec_factory("provider-2"))

    await sandbox.destroy()
    assert await provider.list({}) == []
    again = await provider.create(spec_factory("provider-3"))
    await again.destroy()


async def test_list_filters_by_every_label(provider: WindowsSandboxProvider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("labels-1"))
    try:
        assert await provider.list({"trial": "labels-1"}) != []
        assert await provider.list({"trial": "labels-1", "batch": "other"}) == []
    finally:
        await sandbox.destroy()


async def test_reap_only_matches_and_respects_age(
    provider: WindowsSandboxProvider, spec_factory
) -> None:
    sandbox = await provider.create(spec_factory("reap-1"))
    labels = dict(sandbox.spec.labels)
    assert await provider.reap({"trial": "reap-1", "lease": "other"}, older_than_s=0) == 0
    assert await provider.reap(labels, older_than_s=3600) == 0
    assert await provider.reap(labels, older_than_s=0) == 1
    assert await provider.list({}) == []
    await sandbox.destroy()


async def test_expired_instance_reports_sandbox_lost(
    provider: WindowsSandboxProvider, spec_factory
) -> None:
    sandbox = await provider.create(spec_factory("lifetime-1"))
    await provider.expire_live_sandboxes()
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()


async def test_injected_create_failure_is_retried(
    provider: WindowsSandboxProvider, spec_factory
) -> None:
    provider.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await provider.create(spec_factory("failed-1"))
    retried = await provider.create(spec_factory("failed-2"))
    result = await retried.exec(["true"])
    assert result.exit_code == 0
    await retried.destroy()


@pytest.mark.parametrize(
    "policy",
    [
        NetworkPolicy(mode=NetMode.DOCS_WHITELIST),
        NetworkPolicy(mode=NetMode.PACKAGE_MIRROR),
        NetworkPolicy(mode=NetMode.FULL, allow_hosts=("example.com",)),
        NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",)),
        NetworkPolicy(mode=NetMode.FULL, via_proxy="http://127.0.0.1:3128"),
    ],
    ids=["docs-whitelist", "package-mirror", "hosts", "cidrs", "proxy"],
)
async def test_unenforceable_network_policies_are_rejected(
    provider: WindowsSandboxProvider, spec_factory, policy: NetworkPolicy
) -> None:
    spec = replace(spec_factory("net-reject"), network=policy)
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(spec)
    assert await provider.list({}) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: replace(spec, os=OSFamily.WINDOWS),
        lambda spec: replace(spec, arch=Arch.ARM64),
        lambda spec: replace(spec, template=replace(spec.template, kind="oci")),
        lambda spec: replace(spec, gui=True),
    ],
    ids=["os", "arch", "template-kind", "gui"],
)
async def test_unsupported_specs_are_rejected(
    provider: WindowsSandboxProvider,
    spec_factory,
    mutate: Callable[[SandboxSpec], SandboxSpec],
) -> None:
    with pytest.raises(CapabilityNotSupported):
        await provider.create(mutate(spec_factory("cap-reject")))
    assert await provider.list({}) == []


async def test_gpu_spec_is_rejected(provider: WindowsSandboxProvider, spec_factory) -> None:
    spec = spec_factory("gpu-reject")
    spec = replace(spec, resources=replace(spec.resources, gpus=1))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(spec)


async def test_full_network_is_accepted(provider: WindowsSandboxProvider, spec_factory) -> None:
    spec = replace(spec_factory("net-full"), network=NetworkPolicy(mode=NetMode.FULL))
    sandbox = await provider.create(spec)
    await sandbox.destroy()


async def test_preflight_reports_cli(provider: WindowsSandboxProvider, spec_factory) -> None:
    report = await provider.preflight()
    assert report["dev_only"] is True
    assert report["guest_os"] == "linux"
    assert report["cli_ok"] is True
    assert report["version"].startswith("fake-wsb")
    assert report["live_instances"] == 0
    sandbox = await provider.create(spec_factory("preflight-1"))
    try:
        assert (await provider.preflight())["live_instances"] == 1
    finally:
        await sandbox.destroy()


async def test_preflight_without_executable(tmp_path: Path) -> None:
    provider = WindowsSandboxProvider(
        "missing",
        {"wsb_path": str(tmp_path / "no-such-wsb"), "state_dir": str(tmp_path / "state")},
        _Secrets(),
    )
    with pytest.raises(SpleefError):
        await provider.preflight()


async def test_windows_guest_rejects_linux_spec(tmp_path: Path) -> None:
    provider = WindowsSandboxProvider(
        "windows-guest",
        {"state_dir": str(tmp_path / "state")},
        _Secrets(),
    )
    with pytest.raises(CapabilityNotSupported):
        await provider.create(replace(_minimal_spec(), os=OSFamily.LINUX))


def _minimal_spec() -> SandboxSpec:
    return SandboxSpec(
        trial_id="windows-1",
        lease_id="lease-windows-1",
        role="agent",
        os=OSFamily.WINDOWS,
        arch=Arch.X86_64,
        template=TemplateRef(kind="vhdx", ref="host-image", digest="sha256:" + "0" * 64),
        resources=Resources(cpus=1, memory_mb=1024, disk_mb=1024),
        network=NetworkPolicy(mode=NetMode.OFF),
        max_lifetime_s=600,
    )
