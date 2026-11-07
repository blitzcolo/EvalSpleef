"""Unit tests for DevicePoolProvider and DevicePoolSandbox."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from evalspleef_sbx_device_pool.provider import DevicePool, DevicePoolProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    TransferError,
)
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef

from .conftest import DummySecrets


def _make_spec(
    trial_id: str = "t-1",
    lease_id: str = "l-1",
    os_fam: OSFamily = OSFamily.HARMONY,
    net_mode: NetMode = NetMode.OFF,
    allow_hosts: tuple[str, ...] = (),
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=lease_id,
        role="agent",
        os=os_fam,
        arch=Arch.ARM64,
        template=TemplateRef(kind="device_profile", ref="harmony-6", digest="sha256:000"),
        resources=Resources(cpus=4, memory_mb=8192, disk_mb=32768),
        network=NetworkPolicy(mode=net_mode, allow_hosts=allow_hosts),
        labels={"batch": "b1", "trial": trial_id},
    )


@pytest.mark.asyncio
async def test_device_pool_leasing_and_exhaustion() -> None:
    pool = DevicePool(
        raw_devices=[
            {"serial": "D1", "state": "free", "baseline_packages": ["p1", "p2"]},
            {"serial": "D2", "state": "free", "baseline_packages": ["p1", "p2"]},
        ]
    )

    d1 = await pool.claim_device("lease-1")
    assert d1.serial == "D1"
    assert d1.state == "in_use"

    d2 = await pool.claim_device("lease-2")
    assert d2.serial == "D2"

    with pytest.raises(QuotaExceeded):
        await pool.claim_device("lease-3")


@pytest.mark.asyncio
async def test_return_hygiene_success_and_failure() -> None:
    pool = DevicePool(
        raw_devices=[
            {"serial": "D1", "state": "free", "baseline_packages": ["p1", "p2"]},
        ]
    )

    await pool.claim_device("lease-1")

    # Releasing successfully returns to free
    await pool.release_device("D1", success=True)
    assert pool.devices["D1"].state == "free"
    assert pool.devices["D1"].current_lease is None

    # Re-claim
    await pool.claim_device("lease-2")

    # Release with hygiene failure -> quarantined + incident report
    await pool.release_device(
        "D1",
        success=False,
        observed_packages=["p1", "p2", "malicious_app"],
        observed_hash="badhash",
        reason="unexpected package residue",
        trial_id="t-1",
        lease_id="lease-2",
    )
    assert pool.devices["D1"].state == "quarantined"
    assert pool.devices["D1"].quarantine_reason == "unexpected package residue"
    assert len(pool.incidents) == 1
    assert pool.incidents[0].package_diff == ["malicious_app"]


@pytest.mark.asyncio
async def test_device_pool_provider_create_and_reap(fake_hdc_dir: Path) -> None:
    hdc_bin = fake_hdc_dir / "hdc"
    provider = DevicePoolProvider(
        instance="dev-test",
        config={
            "devices": [
                {"serial": "D1", "state": "free", "baseline_packages": ["com.huawei.system"]},
            ],
            "hdc_path": str(hdc_bin),
        },
        secrets=DummySecrets(),
    )

    # Capability check: unsupported OS
    with pytest.raises(CapabilityNotSupported):
        await provider.create(_make_spec(os_fam=OSFamily.WINDOWS))

    # Policy check: allow_hosts rejected without proxy
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(_make_spec(allow_hosts=("example.com",)))

    # Successful create
    sbx = await provider.create(_make_spec(trial_id="t-1", lease_id="l-1"))
    assert sbx.id == "sbx-dev-t-1-D1"

    # List
    items = await provider.list({"trial": "t-1"})
    assert len(items) == 1
    assert items[0]["device_serial"] == "D1"

    # Reap
    reaped = await provider.reap({"trial": "t-1"}, older_than_s=1000)
    assert reaped == 0

    reaped_now = await provider.reap({"trial": "t-1"}, older_than_s=0)
    assert reaped_now == 1
    assert sbx._destroyed is True

    # Destroy is idempotent
    await sbx.destroy()


@pytest.mark.asyncio
async def test_device_pool_sandbox_exec_and_hdc_whitelist(fake_hdc_dir: Path) -> None:
    hdc_bin = fake_hdc_dir / "hdc"
    provider = DevicePoolProvider(
        instance="dev-test",
        config={
            "devices": [
                {"serial": "D1", "state": "free", "baseline_packages": ["com.huawei.system"]},
            ],
            "hdc_path": str(hdc_bin),
        },
        secrets=DummySecrets(),
    )
    sbx = await provider.create(_make_spec(trial_id="t-2"))

    # Whitelisted hdc action: list targets
    res = await sbx.exec("hdc list targets")
    assert res.exit_code == 0
    assert b"Hate-AL00-001" in res.stdout

    # Whitelisted hdc action: hilog
    res = await sbx.exec("hdc shell hilog")
    assert res.exit_code == 0
    assert b"hilog" in res.stdout

    # Non-whitelisted hdc action: arbitrary root exploit
    res_bad = await sbx.exec("hdc shell rm -rf /system")
    assert res_bad.exit_code != 0
    assert b"Permission denied" in res_bad.stderr

    # Standard shell command in workspace
    res_sh = await sbx.exec("echo hello_world")
    assert res_sh.exit_code == 0
    assert b"hello_world" in res_sh.stdout

    await sbx.destroy()


@pytest.mark.asyncio
async def test_device_pool_sandbox_timeout_kills_tree(fake_hdc_dir: Path) -> None:
    hdc_bin = fake_hdc_dir / "hdc"
    provider = DevicePoolProvider(
        instance="dev-test",
        config={
            "devices": [
                {"serial": "D1", "state": "free", "baseline_packages": ["com.huawei.system"]},
            ],
            "hdc_path": str(hdc_bin),
        },
        secrets=DummySecrets(),
    )
    sbx = await provider.create(_make_spec())

    res = await sbx.exec("sleep 10", timeout_s=0.2)
    assert res.exit_code == 124
    assert res.timed_out is True

    await sbx.destroy()


@pytest.mark.asyncio
async def test_device_pool_file_transfer_and_symlink_guard(fake_hdc_dir: Path) -> None:
    hdc_bin = fake_hdc_dir / "hdc"
    provider = DevicePoolProvider(
        instance="dev-test",
        config={
            "devices": [
                {"serial": "D1", "state": "free", "baseline_packages": ["com.huawei.system"]},
            ],
            "hdc_path": str(hdc_bin),
        },
        secrets=DummySecrets(),
    )
    sbx = await provider.create(_make_spec())

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        local_src = p / "data.txt"
        local_src.write_text("hello device", encoding="utf-8")

        await sbx.upload(local_src, "uploaded/data.txt")
        assert (sbx.workspace / "uploaded/data.txt").read_text(encoding="utf-8") == "hello device"

        local_dst = p / "downloaded.txt"
        await sbx.download("uploaded/data.txt", local_dst)
        assert local_dst.read_text(encoding="utf-8") == "hello device"

        # Escaping symlink test (Invariant I11)
        escape_link = sbx.workspace / "escape.txt"
        escape_link.symlink_to("/etc/passwd")

        escape_dst = p / "escaped.txt"
        with pytest.raises(TransferError):
            await sbx.download("escape.txt", escape_dst)

    await sbx.destroy()
