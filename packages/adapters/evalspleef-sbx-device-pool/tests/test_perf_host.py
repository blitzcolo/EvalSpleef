"""Unit tests for PerfHostProvider and PerfHostSandbox."""

from __future__ import annotations

import hashlib

import pytest
from evalspleef_sbx_device_pool.perf import FakeSystemProbe, PerfHostProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import (
    QuotaExceeded,
    SandboxCreateError,
)
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef

from .conftest import DummySecrets


def _make_perf_spec(trial_id: str = "p-1", lease_id: str = "pl-1") -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=lease_id,
        role="agent",
        os=OSFamily.MACOS,
        arch=Arch.ARM64,
        template=TemplateRef(kind="device_profile", ref="macos-metal", digest="sha256:111"),
        resources=Resources(cpus=8, memory_mb=16384, disk_mb=65536, gpus=1),
        network=NetworkPolicy(mode=NetMode.OFF),
        labels={"batch": "perf-b1", "trial": trial_id},
    )


@pytest.mark.asyncio
async def test_perf_host_system_probe_precondition_check() -> None:
    # High load fails preflight
    bad_probe = FakeSystemProbe(load1=4.5)
    provider = PerfHostProvider("perf-1", {}, DummySecrets(), system_probe=bad_probe)
    with pytest.raises(SandboxCreateError, match=r"load1=4\.50"):
        await provider.create(_make_perf_spec())

    # Low power mode fails preflight
    power_probe = FakeSystemProbe(low_power_mode=True)
    provider_power = PerfHostProvider("perf-2", {}, DummySecrets(), system_probe=power_probe)
    with pytest.raises(SandboxCreateError, match="LowPowerMode=True"):
        await provider_power.create(_make_perf_spec())

    # High temperature fails preflight
    temp_probe = FakeSystemProbe(temperature_celsius=88.0)
    provider_temp = PerfHostProvider("perf-3", {}, DummySecrets(), system_probe=temp_probe)
    with pytest.raises(SandboxCreateError, match=r"temp=88\.0C"):
        await provider_temp.create(_make_perf_spec())


@pytest.mark.asyncio
async def test_perf_host_concurrency_limit_1() -> None:
    probe = FakeSystemProbe()
    provider = PerfHostProvider("perf-main", {}, DummySecrets(), system_probe=probe)

    sbx1 = await provider.create(_make_perf_spec("trial-1"))
    assert sbx1.id == "sbx-perf-trial-1"

    # Second trial raises QuotaExceeded
    with pytest.raises(QuotaExceeded):
        await provider.create(_make_perf_spec("trial-2"))

    # Once sbx1 destroyed, slot is released
    await sbx1.destroy()
    sbx2 = await provider.create(_make_perf_spec("trial-2"))
    assert sbx2.id == "sbx-perf-trial-2"
    await sbx2.destroy()


@pytest.mark.asyncio
async def test_perf_host_signed_artifact_manifest_enforcement() -> None:
    probe = FakeSystemProbe()
    binary_content = b"#!/bin/sh\necho benchmark_passed\n"
    binary_hash = hashlib.sha256(binary_content).hexdigest()

    manifest = {"benchmark_app": binary_hash}
    provider = PerfHostProvider(
        "perf-manifest",
        {"artifact_manifest": manifest},
        DummySecrets(),
        system_probe=probe,
    )
    sbx = await provider.create(_make_perf_spec())

    # Place binary in sandbox workspace
    app_path = sbx.workspace / "benchmark_app"
    app_path.write_bytes(binary_content)
    app_path.chmod(0o755)

    # Executing signed binary succeeds
    res_signed = await sbx.exec("./benchmark_app")
    assert res_signed.exit_code == 0
    assert b"benchmark_passed" in res_signed.stdout

    # Tamper with binary content -> rejected as unsigned
    app_path.write_bytes(b"#!/bin/sh\necho tampered\n")
    res_tampered = await sbx.exec("./benchmark_app")
    assert res_tampered.exit_code == 126
    assert b"unsigned artifact rejected" in res_tampered.stderr

    await sbx.destroy()


@pytest.mark.asyncio
async def test_perf_host_timeout_and_reap() -> None:
    probe = FakeSystemProbe()
    provider = PerfHostProvider("perf-timeout", {}, DummySecrets(), system_probe=probe)
    sbx = await provider.create(_make_perf_spec("trial-timeout"))

    res = await sbx.exec("sleep 10", timeout_s=0.2)
    assert res.exit_code == 124
    assert res.timed_out is True

    items = await provider.list({"trial": "trial-timeout"})
    assert len(items) == 1

    reaped = await provider.reap({"trial": "trial-timeout"}, older_than_s=0)
    assert reaped == 1
    assert sbx._destroyed is True
