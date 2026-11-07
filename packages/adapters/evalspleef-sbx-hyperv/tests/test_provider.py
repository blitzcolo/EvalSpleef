"""Provider-level tests: capabilities, spec validation, slots, labels and the VM lifecycle.

The host is a ``FakeHostShell``: these tests prove what the provider asks the
host to do and how it reacts to host failures, not that Hyper-V behaves so.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_hyperv._fake_host_shell import FakeHostShell
from evalspleef_sbx_hyperv.provider import HyperVProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
)
from evalspleef.spi.sandbox import Resources, Sandbox, SandboxSpec, TemplateRef

SpecFactory = Callable[..., SandboxSpec]
ProviderFactory = Callable[..., HyperVProvider]
GOLDEN_DIGEST = "sha256:" + "a" * 64


def golden_path(host_root: Path) -> str:
    return str(host_root / "golden" / "windows-golden.vhdx")


def _entries(state_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state_dir / "sandboxes").glob("*.json"))
    ]


def _disk_files(host_root: Path) -> list[str]:
    vhds = host_root / "vhds"
    return sorted(path.name for path in vhds.iterdir()) if vhds.exists() else []


def _expire_entry(state_dir: Path, vm_name: str) -> None:
    path = state_dir / "sandboxes" / f"{vm_name}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["expires_at"] = 1.0
    path.write_text(json.dumps(entry), encoding="utf-8")


async def test_capabilities_are_exact(provider: HyperVProvider) -> None:
    caps = provider.capabilities()
    assert caps.os_families == frozenset({OSFamily.WINDOWS, OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.X86_64})
    assert caps.isolation == Isolation.L3
    assert caps.net_modes == frozenset(
        {NetMode.OFF, NetMode.FULL, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
    )
    assert caps.allowlist_kinds == frozenset({"ipv4", "cidr"})
    assert caps.dynamic_network_policy is False
    assert caps.streaming is True
    assert caps.stdin is True
    assert caps.snapshot == "memory"
    assert caps.snapshot_fanout is True
    assert caps.pause_resume is False
    assert caps.gui == "rdp"
    assert caps.gpu is False
    assert caps.port_expose is False
    assert caps.host_mounts is False
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.max_concurrency == 4
    assert caps.max_lifetime_s == 14400


async def test_max_concurrency_follows_config(provider_factory: ProviderFactory) -> None:
    assert provider_factory(max_concurrency=7).capabilities().max_concurrency == 7


async def test_create_follows_the_documented_hyperv_sequence(
    provider: HyperVProvider,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    host_root: Path,
    state_dir: Path,
) -> None:
    sandbox = await provider.create(spec_factory("seq-1"))
    try:
        verbs = [script.split()[0] for script in fake_shell.commands]
        assert verbs[:6] == [
            "New-VHD",
            "New-VM",
            "Set-VMProcessor",
            "Set-VMMemory",
            "Connect-VMNetworkAdapter",
            "Start-VM",
        ]
        assert set(verbs[6:]) == {"(Get-VMNetworkAdapter"}
        vhdx = f"{host_root / 'vhds' / sandbox.id}.vhdx"
        assert fake_shell.scripts("New-VHD") == [
            f"New-VHD -Path '{vhdx}' -ParentPath '{golden_path(host_root)}' -Differencing"
        ]
        assert fake_shell.scripts("New-VM") == [
            f"New-VM -Name '{sandbox.id}' -Generation 2 -VHDPath '{vhdx}'"
        ]
        assert fake_shell.scripts("Set-VMProcessor") == [
            f"Set-VMProcessor -VMName '{sandbox.id}' -Count 2"
        ]
        assert fake_shell.scripts("Set-VMMemory") == [
            f"Set-VMMemory -VMName '{sandbox.id}' -StartupBytes 4096MB -DynamicMemoryEnabled $false"
        ]
        vm = fake_shell.vms[sandbox.id]
        assert (vm.running, vm.cpus, vm.memory_mb, vm.switch) == (True, 2, 4096, "spleef-private")
        assert provider.is_live(sandbox.id)
    finally:
        await sandbox.destroy()
    assert fake_shell.vms == {}
    assert _disk_files(host_root) == []
    assert _entries(state_dir) == []


@pytest.mark.parametrize(
    ("network", "kwargs", "switch", "acl"),
    [
        (NetMode.OFF, {}, "spleef-private", []),
        (NetMode.FULL, {}, "spleef-external", []),
        (
            NetMode.DOCS_WHITELIST,
            {"via_proxy": "203.0.113.9:3128"},
            "spleef-internal",
            ["203.0.113.9"],
        ),
        (
            NetMode.DOCS_WHITELIST,
            {"via_proxy": "http://10.0.0.5:3128"},
            "spleef-internal",
            ["10.0.0.5"],
        ),
        (
            NetMode.PACKAGE_MIRROR,
            {"allow_cidrs": ("203.0.113.0/24", "198.51.100.7/32")},
            "spleef-internal",
            ["203.0.113.0/24", "198.51.100.7/32"],
        ),
        (
            NetMode.PACKAGE_MIRROR,
            {"allow_hosts": ("pypi.org",), "via_proxy": "203.0.113.9:3128"},
            "spleef-internal",
            ["203.0.113.9"],
        ),
    ],
)
async def test_network_mode_selects_the_vswitch_and_acl(
    provider: HyperVProvider,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    network: NetMode,
    kwargs: dict[str, Any],
    switch: str,
    acl: list[str],
) -> None:
    sandbox = await provider.create(spec_factory("net", network=network, **kwargs))
    try:
        vm = fake_shell.vms[sandbox.id]
        assert vm.switch == switch
        assert vm.acl_addresses == acl
        acl_scripts = fake_shell.scripts("Add-VMNetworkAdapterAcl")
        if acl:
            assert acl_scripts == [
                f"Add-VMNetworkAdapterAcl -VMName '{sandbox.id}' "
                f"-RemoteAddress {','.join(acl)} -Direction Both -Action Allow"
            ]
        else:
            assert acl_scripts == []
    finally:
        await sandbox.destroy()


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"os": OSFamily.MACOS}, CapabilityNotSupported),
        ({"arch": Arch.ARM64}, CapabilityNotSupported),
        ({"max_lifetime_s": 10**9}, CapabilityNotSupported),
        ({"env": {"TASK_ID": "leak"}}, CapabilityNotSupported),
        ({"network": NetMode.OFF, "allow_cidrs": ("10.0.0.0/8",)}, PolicyNotEnforceable),
        ({"network": NetMode.FULL, "allow_hosts": ("pypi.org",)}, PolicyNotEnforceable),
        ({"network": NetMode.OFF, "via_proxy": "10.0.0.5:3128"}, PolicyNotEnforceable),
        ({"network": NetMode.DOCS_WHITELIST}, PolicyNotEnforceable),
        (
            {"network": NetMode.DOCS_WHITELIST, "allow_hosts": ("pypi.org",)},
            PolicyNotEnforceable,
        ),
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
        (
            {"network": NetMode.DOCS_WHITELIST, "allow_cidrs": ("not-a-cidr",)},
            PolicyNotEnforceable,
        ),
    ],
)
async def test_create_rejects_what_it_cannot_honour_without_touching_the_host(
    provider: HyperVProvider,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    overrides: dict[str, Any],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        await provider.create(spec_factory("reject", **overrides))
    assert fake_shell.commands == []


async def test_create_rejects_gpu_and_non_vhdx_template(
    provider: HyperVProvider, spec_factory: SpecFactory, fake_shell: FakeHostShell
) -> None:
    spec = spec_factory("reject-kinds")
    with pytest.raises(CapabilityNotSupported):
        await provider.create(
            replace(spec, resources=Resources(cpus=2, memory_mb=4096, disk_mb=40960, gpus=1))
        )
    with pytest.raises(CapabilityNotSupported):
        await provider.create(
            replace(spec, template=TemplateRef(kind="oci", ref="x", digest=GOLDEN_DIGEST))
        )
    assert fake_shell.commands == []


async def test_create_over_max_concurrency_raises_quota_until_a_slot_frees(
    provider_factory: ProviderFactory, spec_factory: SpecFactory
) -> None:
    provider = provider_factory(max_concurrency=1)
    first = await provider.create(spec_factory("slot-1"))
    with pytest.raises(QuotaExceeded):
        await provider.create(spec_factory("slot-2"))
    await first.destroy()
    second = await provider.create(spec_factory("slot-2"))
    await second.destroy()


async def test_concurrent_creates_never_overshoot_the_limit(
    provider_factory: ProviderFactory, spec_factory: SpecFactory
) -> None:
    provider = provider_factory(max_concurrency=2)
    results = await asyncio.gather(
        *(provider.create(spec_factory(f"burst-{index}")) for index in range(3)),
        return_exceptions=True,
    )
    created = [item for item in results if isinstance(item, Sandbox)]
    rejected = [item for item in results if isinstance(item, QuotaExceeded)]
    try:
        assert len(created) == 2
        assert len(rejected) == 1
    finally:
        for sandbox in created:
            await sandbox.destroy()


@pytest.mark.parametrize(
    "operation",
    [
        "New-VHD",
        "New-VM",
        "Set-VMProcessor",
        "Set-VMMemory",
        "Connect-VMNetworkAdapter",
        "Start-VM",
    ],
)
async def test_a_failing_host_step_leaves_nothing_behind(
    provider: HyperVProvider,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    host_root: Path,
    state_dir: Path,
    operation: str,
) -> None:
    fake_shell.fail(operation)
    with pytest.raises(SandboxCreateError) as caught:
        await provider.create(spec_factory("broken"))
    assert caught.value.detail["operation"] == operation
    assert fake_shell.vms == {}
    assert _disk_files(host_root) == []
    assert _entries(state_dir) == []


async def test_create_gives_up_when_openssh_never_answers(
    provider_factory: ProviderFactory,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    host_root: Path,
) -> None:
    fake_shell.no_ip = True
    provider = provider_factory(start_timeout_s=0.3)
    with pytest.raises(SandboxCreateError, match="OpenSSH"):
        await provider.create(spec_factory("no-ssh"))
    assert fake_shell.vms == {}
    assert _disk_files(host_root) == []


async def test_registry_carries_labels_and_the_lifetime_guard(
    provider: HyperVProvider, spec_factory: SpecFactory, state_dir: Path
) -> None:
    sandbox = await provider.create(spec_factory("reg-1"))
    try:
        (entry,) = _entries(state_dir)
        assert entry["id"] == sandbox.id
        assert entry["labels"]["batch"] == "adapter-test"
        assert entry["labels"]["spleef.provider"] == "test-hyperv"
        assert entry["labels"]["spleef.trial"] == "reg-1"
        assert entry["labels"]["spleef.lease"] == "lease-reg-1"
        assert entry["expires_at"] - entry["created_at"] == pytest.approx(600, abs=2)
        assert (entry["os"], entry["cpus"], entry["memory_mb"]) == ("windows", 2, 4096)
        assert entry["digest"] == GOLDEN_DIGEST
    finally:
        await sandbox.destroy()


async def test_list_and_reap_work_on_labels(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> None:
    first = await provider.create(spec_factory("reap-a"))
    second = await provider.create(spec_factory("reap-b", os=OSFamily.LINUX))
    try:
        listed = await provider.list({"batch": "adapter-test"})
        assert {entry["id"] for entry in listed} == {first.id, second.id}
        assert [entry["id"] for entry in await provider.list({"trial": "reap-b"})] == [second.id]
        assert await provider.reap({"trial": "reap-a"}, older_than_s=10**6) == 0
        assert await provider.reap({"trial": "reap-a"}, older_than_s=0) == 1
        remaining = await provider.list({"batch": "adapter-test"})
        assert [entry["id"] for entry in remaining] == [second.id]
        with pytest.raises(SandboxLost):
            await first.exec(["true"])
    finally:
        await second.destroy()


async def test_list_hides_vms_that_vanished_from_the_host(
    provider: HyperVProvider, spec_factory: SpecFactory, fake_shell: FakeHostShell
) -> None:
    sandbox = await provider.create(spec_factory("ghost"))
    try:
        del fake_shell.vms[sandbox.id]
        assert await provider.list({"trial": "ghost"}) == []
    finally:
        await sandbox.destroy()


async def test_ttl_sweep_destroys_expired_sandboxes(
    provider: HyperVProvider, spec_factory: SpecFactory, state_dir: Path
) -> None:
    sandbox = await provider.create(spec_factory("ttl-1"))
    _expire_entry(state_dir, sandbox.id)
    await provider._sweep_once()
    assert not provider.is_live(sandbox.id)
    assert await provider.list({"trial": "ttl-1"}) == []
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_destroy_is_stop_remove_delete_and_idempotent(
    provider: HyperVProvider,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    host_root: Path,
) -> None:
    sandbox = await provider.create(spec_factory("destroy-1"))
    await sandbox.destroy()
    await sandbox.destroy()
    script = fake_shell.scripts("Remove-VM")[0]
    vhdx = f"{host_root / 'vhds' / sandbox.id}.vhdx"
    assert f"Stop-VM -Name '{sandbox.id}' -TurnOff -Force" in script
    assert f"Remove-VM -Name '{sandbox.id}' -Force" in script
    assert f"Remove-Item -LiteralPath '{vhdx}' -Recurse -Force" in script
    assert not provider.is_live(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_preflight_reports_host_facts(provider: HyperVProvider) -> None:
    report = await provider.preflight()
    assert report["provider"] == "hyperv/test-hyperv"
    assert report["powershell_version"] == "5.1.26100.1 (fake)"
    assert report["hyperv_module"] == "Hyper-V"
    assert report["max_concurrency"] == 4
    assert report["live_sandboxes"] == 0
    assert report["transfer"] == "sftp"


async def test_preflight_fails_when_the_hyperv_module_is_missing(
    provider: HyperVProvider, fake_shell: FakeHostShell
) -> None:
    fake_shell.fail("Get-Command Get-VM")
    with pytest.raises(SpleefError, match="Hyper-V PowerShell module"):
        await provider.preflight()


@pytest.mark.parametrize("injected", [SandboxCreateError, SandboxCreateError("boom")])
async def test_injected_create_failure_then_retry(
    provider: HyperVProvider, spec_factory: SpecFactory, injected: Any
) -> None:
    provider.fail_next_creates(1, injected)
    with pytest.raises(SandboxCreateError):
        await provider.create(spec_factory("fail-1"))
    retried = await provider.create(spec_factory("fail-1-retry"))
    try:
        assert (await retried.exec(["echo", "ok"])).stdout == b"ok\n"
    finally:
        await retried.destroy()


async def test_lose_and_expire_remove_live_vms(
    provider: HyperVProvider, spec_factory: SpecFactory, fake_shell: FakeHostShell
) -> None:
    lost = await provider.create(spec_factory("lose-1"))
    await provider.lose(lost.id)
    assert not provider.is_live(lost.id)
    assert lost.id not in fake_shell.vms
    other = await provider.create(spec_factory("expire-1"))
    await provider.expire_live_sandboxes()
    assert not provider.is_live(other.id)
    assert fake_shell.vms == {}


async def test_describe_reports_the_template_digest_actually_used(
    sandbox: Any,
) -> None:
    facts = await sandbox.describe()
    assert facts["digest"] == GOLDEN_DIGEST
    assert (facts["os"], facts["cpus"], facts["memory_mb"]) == ("windows", 2, 4096)
    assert (facts["network"], facts["host"]) == ("off", "hyper-v")


async def test_snapshot_restores_state_into_independent_instances(
    provider: HyperVProvider, spec_factory: SpecFactory, fake_shell: FakeHostShell
) -> None:
    source = await provider.create(spec_factory("snap-src"))
    try:
        written = await source.exec(["sh", "-c", "printf body > .marker"])
        assert written.exit_code == 0
        snapshot_id = await source.snapshot("tpl")
    finally:
        await source.destroy()
    assert re.fullmatch(r"[A-Za-z0-9_.@-]+", snapshot_id)
    assert snapshot_id.endswith("@tpl")
    assert fake_shell.scripts("Checkpoint-VM")[0].endswith("-SnapshotName 'spleef-tpl'")

    first = await provider.create_from_snapshot(snapshot_id, spec_factory("snap-a"))
    second = await provider.create_from_snapshot(snapshot_id, spec_factory("snap-b"))
    try:
        assert "spleef-tpl.vhdx" in fake_shell.scripts("New-VHD")[-1]
        assert (await first.exec(["cat", ".marker"])).stdout == b"body"
        await first.exec(["sh", "-c", "printf diverged > .marker"])
        assert (await second.exec(["cat", ".marker"])).stdout == b"body"
    finally:
        await first.destroy()
        await second.destroy()


async def test_snapshot_ids_are_filename_safe_and_validated(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> None:
    source = await provider.create(spec_factory("snap-name"))
    try:
        snapshot_id = await source.snapshot("a b/c")
    finally:
        await source.destroy()
    assert snapshot_id.endswith("@a-b-c")
    with pytest.raises(SandboxCreateError, match="unknown"):
        await provider.create_from_snapshot("spleef-x@nope", spec_factory("snap-x"))
    with pytest.raises(SandboxCreateError, match="malformed"):
        await provider.create_from_snapshot("../../etc/passwd", spec_factory("snap-y"))
    with pytest.raises(CapabilityNotSupported):
        await provider.create_from_snapshot(snapshot_id, spec_factory("snap-z", os=OSFamily.LINUX))


async def test_invalid_transfer_config_is_refused(provider_factory: ProviderFactory) -> None:
    with pytest.raises(ValueError, match="transfer"):
        provider_factory(transfer="rsync")
