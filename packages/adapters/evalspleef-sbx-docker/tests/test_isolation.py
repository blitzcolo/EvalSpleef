"""Isolation profile, YAML artifact and probe script checks (no daemon)."""

from __future__ import annotations

import re
from pathlib import Path

from evalspleef_sbx_docker import isolation
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.caps import Isolation

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
PROBE_PATH = PACKAGE_DIR / "probes" / "linux.sh"

INVARIANT_IDS = [f"I{index}" for index in range(1, 17)]


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


def test_profile_covers_all_invariants() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "docker"
    assert profile.declared_isolation == Isolation.L1
    assert set(profile.invariants) == set(INVARIANT_IDS)
    assert len(profile.invariants) == 16
    assert not profile.waivers()
    for entry in profile.invariants.values():
        assert entry.how
        assert entry.probe


def test_yaml_matches_python_profile() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    yaml_keys = set(re.findall(r"^  (I\d+):$", text, flags=re.MULTILINE))
    profile = isolation.isolation_profile()
    assert yaml_keys == set(profile.invariants)
    assert "declared_isolation: L1" in text


def test_linux_probe_script_checks_core_invariants() -> None:
    script = isolation.linux_probe_script()
    for needle in [
        "id -u",
        "sudo -n true",
        "findmnt -no OPTIONS /",
        "findmnt -no FSTYPE /tmp",
        "docker.sock",
        "CapEff",
        "cpu.max",
        "memory.max",
        "pids.max",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in script, f"probe script misses {needle!r}"
    assert PROBE_PATH.read_text(encoding="utf-8") == script


def test_runsc_config_raises_declared_isolation() -> None:
    provider = DockerProvider("test", {"runtime": "runsc"}, _Secrets())
    assert provider.capabilities().isolation == Isolation.L2
    assert provider.isolation_profile().declared_isolation == Isolation.L2
