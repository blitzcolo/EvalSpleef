"""Isolation profile, YAML artifact and probe script checks (no VM needed)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from evalspleef_sbx_tart import isolation

from evalspleef.spi.caps import Isolation, OSFamily

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
MACOS_PROBE = PACKAGE_DIR / "probes" / "macos.sh"
LINUX_PROBE = PACKAGE_DIR / "probes" / "linux.sh"
INVARIANT_IDS = {f"I{index}" for index in range(1, 17)}


def test_profile_covers_all_invariants_and_is_l3() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "tart"
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    assert set(profile.invariants) == INVARIANT_IDS
    for entry in profile.invariants.values():
        assert entry.how
        assert entry.probe


def test_every_waiver_carries_reason_compensation_and_expiry() -> None:
    profile = isolation.isolation_profile()
    waivers = profile.waivers()
    assert "I2" in waivers
    for key, entry in waivers.items():
        assert entry.reason, key
        assert entry.compensating, key
        assert entry.approved_by, key
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.expires or ""), key


def test_yaml_matches_the_python_profile() -> None:
    document = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    profile = isolation.isolation_profile()
    assert document["provider"] == "tart"
    assert document["declared_isolation"] == "L3"
    assert document["verified_isolation"] is None
    assert set(document["invariants"]) == set(profile.invariants)
    for key, entry in profile.invariants.items():
        loaded = document["invariants"][key]
        assert loaded["how"] == entry.how
        assert loaded["probe"] == entry.probe
        assert loaded["waived"] is entry.waived
        if entry.waived:
            assert loaded["reason"] == entry.reason
            assert loaded["compensating"] == entry.compensating
            assert loaded["approved_by"] == entry.approved_by
            assert str(loaded["expires"]) == entry.expires


def test_macos_probe_script_checks_the_required_facts() -> None:
    script = isolation.probe_script(OSFamily.MACOS)
    for needle in ["id -u", "sudo -n true", "mount", "scutil --get ComputerName", "example.com"]:
        assert needle in script, f"probe script misses {needle!r}"
    assert "CHECK " in script
    assert "RESULT pass" in script
    assert "RESULT fail" in script
    assert MACOS_PROBE.read_text(encoding="utf-8") == script


def test_linux_probe_script_is_shipped() -> None:
    script = isolation.probe_script(OSFamily.LINUX)
    assert "id -u" in script
    assert "sudo -n true" in script
    assert "example.com" in script
    assert LINUX_PROBE.read_text(encoding="utf-8") == script


def test_windows_has_no_probe_set() -> None:
    with pytest.raises(ValueError):
        isolation.probe_script(OSFamily.WINDOWS)
