"""Tests for the isolation profile and OS preflight probes."""

from __future__ import annotations

from evalspleef_sbx_device_pool.isolation import (
    device_pool_isolation_profile,
    load_profile_yaml,
    perf_host_isolation_profile,
    read_probe_script,
)

from evalspleef.spi.caps import Isolation


def test_device_pool_isolation_profile() -> None:
    profile = device_pool_isolation_profile()
    assert profile.provider == "device-pool"
    assert profile.declared_isolation == Isolation.L4
    assert len(profile.invariants) == 16
    assert set(profile.invariants.keys()) == {f"I{i}" for i in range(1, 17)}

    # Check waiver on I2
    waivers = profile.waivers()
    assert "I2" in waivers
    entry = waivers["I2"]
    assert entry.waived is True
    assert entry.reason is not None
    assert entry.compensating is not None
    assert entry.approved_by == "eval-lead"
    assert entry.expires is not None


def test_perf_host_isolation_profile() -> None:
    profile = perf_host_isolation_profile()
    assert profile.provider == "perf-host"
    assert profile.declared_isolation == Isolation.L4
    assert len(profile.invariants) == 16
    assert set(profile.invariants.keys()) == {f"I{i}" for i in range(1, 17)}

    waivers = profile.waivers()
    assert "I2" in waivers


def test_profile_yaml_matches_code() -> None:
    raw = load_profile_yaml()
    assert raw["provider"] == "device-pool"
    assert raw["declared_isolation"] == "L4"
    invariants = raw["invariants"]
    assert len(invariants) == 16
    assert invariants["I2"]["waived"] is True
    assert invariants["I1"]["waived"] is False


def test_probe_scripts_loadable() -> None:
    for os_name in ("harmony.sh", "macos.sh", "linux.sh"):
        content = read_probe_script(os_name)
        assert "RESULT" in content
        assert "CHECK" in content
