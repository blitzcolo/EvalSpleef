"""Isolation profile, YAML artifact and probe script checks (no VM needed)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from evalspleef_sbx_hyperv import isolation
from evalspleef_sbx_hyperv.provider import HyperVProvider

from evalspleef.spi.caps import Isolation, OSFamily
from evalspleef.spi.sandbox import ExecResult, OutputCallback, Sandbox

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
WINDOWS_PROBE = PACKAGE_DIR / "probes" / "windows.ps1"
LINUX_PROBE = PACKAGE_DIR / "probes" / "linux.sh"
INVARIANT_IDS = {f"I{index}" for index in range(1, 17)}


class RecordingSandbox(Sandbox):
    """A sandbox double that records exec calls and answers with a canned result."""

    def __init__(self) -> None:
        self.id = "recording"
        self.calls: list[dict[str, Any]] = []

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        self.calls.append({"cmd": cmd, "stdin": stdin, "timeout_s": timeout_s})
        return ExecResult(
            exit_code=0, stdout=b"RESULT pass\n", stderr=b"", timed_out=False, duration_s=0.0
        )

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        raise NotImplementedError

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        raise NotImplementedError

    async def destroy(self) -> None:
        return None


def test_profile_covers_all_invariants_and_is_l3() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "hyperv"
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    assert set(profile.invariants) == INVARIANT_IDS
    for entry in profile.invariants.values():
        assert entry.how
        assert entry.probe


async def test_provider_returns_the_shipped_profile(provider: HyperVProvider) -> None:
    assert provider.isolation_profile() == isolation.isolation_profile()


def test_every_waiver_carries_reason_compensation_approval_and_expiry() -> None:
    waivers = isolation.isolation_profile().waivers()
    assert set(waivers) == {"I2"}
    for key, entry in waivers.items():
        assert entry.reason, key
        assert entry.compensating, key
        assert entry.approved_by, key
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.expires or ""), key


def test_yaml_matches_the_python_profile() -> None:
    document = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    profile = isolation.isolation_profile()
    assert document["provider"] == "hyperv"
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


def test_windows_probe_script_checks_the_required_facts() -> None:
    script = isolation.probe_script(OSFamily.WINDOWS)
    for needle in [
        "whoami /groups",
        "Administrators",
        "Get-SmbShare",
        "Get-NetFirewallProfile",
        "DefaultOutboundAction",
        "Block",
        "Test-NetConnection",
        "TcpTestSucceeded",
        "Get-ChildItem Env:",
        "SPLEEF_NET_MODE",
        "CHECK ",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in script, f"windows probe script misses {needle!r}"
    assert WINDOWS_PROBE.read_text(encoding="utf-8") == script


def test_linux_probe_script_is_shipped() -> None:
    script = isolation.probe_script(OSFamily.LINUX)
    assert "id -u" in script
    assert "sudo -n true" in script
    assert "RESULT pass" in script
    assert LINUX_PROBE.read_text(encoding="utf-8") == script


def test_macos_has_no_probe_set() -> None:
    with pytest.raises(ValueError):
        isolation.probe_script(OSFamily.MACOS)


async def test_windows_probe_runs_as_one_powershell_command() -> None:
    sandbox = RecordingSandbox()
    code, stdout = await isolation.run_isolation_probes(sandbox, OSFamily.WINDOWS, timeout_s=12.0)
    assert (code, stdout) == (0, b"RESULT pass\n")
    (call,) = sandbox.calls
    assert call["cmd"] == isolation.probe_script(OSFamily.WINDOWS)
    assert call["stdin"] is None
    assert call["timeout_s"] == 12.0


async def test_linux_probe_is_fed_over_stdin() -> None:
    sandbox = RecordingSandbox()
    await isolation.run_isolation_probes(sandbox, OSFamily.LINUX)
    (call,) = sandbox.calls
    assert call["cmd"] == ["/bin/sh", "-s"]
    assert call["stdin"] == isolation.probe_script(OSFamily.LINUX).encode("utf-8")
