"""Isolation profile, YAML artefact and probe script checks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from evalspleef_sbx_apple_container import isolation
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

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
    assert profile.provider == "apple-container"
    assert profile.declared_isolation is Isolation.L3
    assert set(profile.invariants) == set(INVARIANT_IDS)
    assert len(profile.invariants) == 16
    for entry in profile.invariants.values():
        assert entry.how
        assert entry.probe


def test_waivers_are_complete() -> None:
    waivers = isolation.isolation_profile().waivers()
    assert waivers
    for entry in waivers.values():
        assert entry.reason
        assert entry.compensating
        assert entry.approved_by
        assert entry.expires and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.expires)


def test_provider_returns_the_shipped_profile() -> None:
    provider = AppleContainerProvider("test", {}, _Secrets())
    profile = provider.isolation_profile()
    assert profile.declared_isolation is Isolation.L3
    assert set(profile.invariants) == set(INVARIANT_IDS)


def test_yaml_matches_python_profile() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    yaml_keys = set(re.findall(r"^  (I\d+):$", text, flags=re.MULTILINE))
    profile = isolation.isolation_profile()
    assert yaml_keys == set(profile.invariants)
    assert "declared_isolation: L3" in text
    assert "waived: true" in text
    for entry in profile.waivers().values():
        assert str(entry.reason) in text.replace("\n      ", " ")


def test_yaml_entries_match_python_profile() -> None:
    document = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    profile = isolation.isolation_profile()
    assert document["declared_isolation"] == profile.declared_isolation.name
    for key, entry in profile.invariants.items():
        shipped = document["invariants"][key]
        assert shipped["how"] == entry.how, key
        assert shipped["probe"] == entry.probe, key
        assert shipped["waived"] == entry.waived, key


def test_preflight_invariants_have_executable_probes() -> None:
    profile = isolation.isolation_profile()
    missing = [f"I{n}" for n in range(1, 10) if profile.invariants[f"I{n}"].command is None]
    assert missing == []


def test_linux_probe_script_checks_core_invariants() -> None:
    script = isolation.linux_probe_script()
    for needle in [
        "id -u",
        "sudo -n true",
        "findmnt -no OPTIONS /",
        "findmnt -no FSTYPE /workspace",
        "container.sock",
        "CapEff",
        "cpu.max",
        "memory.max",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in script, f"probe script misses {needle!r}"
    assert PROBE_PATH.read_text(encoding="utf-8") == script


async def test_probe_script_runs_through_exec(sandbox) -> None:
    script = isolation.linux_probe_script().encode()
    result = await sandbox.exec("sh", stdin=script, timeout_s=30)
    assert result.exit_code == 0, result.stdout.decode() + result.stderr.decode()
    assert b"RESULT pass" in result.stdout
    assert b"CHECK cgroup-cpu pass" in result.stdout
    assert b"CHECK neutral-hostname pass" in result.stdout
