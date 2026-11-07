"""Isolation profile, YAML artifact and probe-script checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from evalspleef_sbx_libvirt import isolation
from evalspleef_sbx_libvirt.provider import LibvirtProvider

from evalspleef.spi.caps import Isolation, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
LINUX_PROBE = PACKAGE_DIR / "probes" / "linux.sh"
WINDOWS_PROBE = PACKAGE_DIR / "probes" / "windows.ps1"
INVARIANT_IDS = [f"I{index}" for index in range(1, 17)]


def test_profile_covers_all_invariants() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "libvirt"
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    assert set(profile.invariants) == set(INVARIANT_IDS)
    assert len(profile.invariants) == 16
    for entry in profile.invariants.values():
        assert entry.how


def test_waivers_are_complete() -> None:
    waivers = isolation.isolation_profile().waivers()
    assert waivers, "the profile should record at least the writable-root waiver"
    for key, entry in waivers.items():
        assert entry.reason, f"{key} has no reason"
        assert entry.compensating, f"{key} has no compensating control"
        assert entry.approved_by, f"{key} has no approver"
        assert entry.expires, f"{key} has no expiry"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.expires or "")


def test_yaml_matches_python_profile() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    yaml_keys = set(re.findall(r"^  (I\d+):$", text, flags=re.MULTILINE))
    profile = isolation.isolation_profile()
    assert yaml_keys == set(profile.invariants)
    assert "declared_isolation: L3" in text
    assert "provider: libvirt" in text


def test_probe_scripts_ship_for_linux_and_windows() -> None:
    linux = isolation.probe_script(OSFamily.LINUX)
    for needle in [
        "id -u",
        "sudo -n true",
        "private-net",
        "no-host-mounts",
        "neutral-hostname",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in linux, f"linux probe misses {needle!r}"
    assert LINUX_PROBE.read_text(encoding="utf-8") == linux

    windows = isolation.probe_script(OSFamily.WINDOWS)
    for needle in [
        "WindowsPrincipal",
        "Get-SmbShare",
        "Get-NetFirewallProfile",
        "Get-ChildItem Env:",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in windows, f"windows probe misses {needle!r}"
    assert WINDOWS_PROBE.read_text(encoding="utf-8") == windows


def test_probe_script_for_unsupported_os_raises() -> None:
    with pytest.raises(CapabilityNotSupported):
        isolation.probe_script(OSFamily.MACOS)


async def test_linux_probe_runs_through_exec(provider: LibvirtProvider, sandbox) -> None:
    result = await provider.run_probes(sandbox)
    assert result["passed"] is True, result["stdout"] + result["stderr"]
    assert result["exit_code"] == 0
    joined = "\n".join(result["checks"])
    assert "CHECK non-root pass" in joined
    assert "CHECK neutral-env pass" in joined
    assert "CHECK cpu-limit pass" in joined
