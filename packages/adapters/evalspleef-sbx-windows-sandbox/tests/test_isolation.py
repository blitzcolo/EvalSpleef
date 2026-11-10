"""Isolation profile, YAML artifact and Windows probe script checks (no host needed)."""

from __future__ import annotations

import re
from pathlib import Path

from evalspleef_sbx_windows_sandbox import isolation
from evalspleef_sbx_windows_sandbox.provider import WindowsSandboxProvider

from evalspleef.spi.caps import Isolation
from evalspleef.spi.sandbox import ExecResult, Sandbox

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
PROBE_PATH = PACKAGE_DIR / "probes" / "windows.ps1"

INVARIANT_IDS = [f"I{index}" for index in range(1, 17)]


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


class _ProbeSandbox(Sandbox):
    """Stands in for a Windows guest so the probe plumbing can be checked."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, cmd, **kwargs: object) -> ExecResult:  # type: ignore[override]
        self.commands.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return ExecResult(
            exit_code=0,
            stdout=b"CHECK I6 PASS non-admin\nCHECK I4 FAIL egress open\n",
            stderr=b"",
            timed_out=False,
            duration_s=0.1,
        )

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        raise NotImplementedError

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        raise NotImplementedError

    async def destroy(self) -> None:
        return None


def test_profile_covers_all_invariants() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "windows-sandbox"
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    assert set(profile.invariants) == set(INVARIANT_IDS)
    for entry in profile.invariants.values():
        assert entry.how


def test_waivers_carry_reason_compensation_and_expiry() -> None:
    profile = isolation.isolation_profile()
    waivers = profile.waivers()
    assert waivers, "Windows Sandbox cannot meet every invariant without a waiver"
    for invariant, entry in waivers.items():
        assert entry.reason, invariant
        assert entry.compensating, invariant
        assert entry.approved_by, invariant
        assert entry.expires, invariant


def test_yaml_matches_python_profile() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    yaml_keys = set(re.findall(r"^  (I\d+):$", text, flags=re.MULTILINE))
    profile = isolation.isolation_profile()
    assert yaml_keys == set(profile.invariants)
    assert "declared_isolation: L3" in text
    for invariant in profile.waivers():
        block = re.search(
            rf"^  {invariant}:$(.*?)(?=^  I\d+:$)", text, flags=re.MULTILINE | re.DOTALL
        )
        assert block is not None, invariant
        for field in ("reason:", "compensating:", "approved_by:", "expires:"):
            assert field in block.group(1), f"{invariant} misses {field}"


def test_windows_probe_script_checks_the_guest() -> None:
    script = isolation.windows_probe_script()
    for needle in [
        "whoami /groups",
        "S-1-5-32-544",
        "Get-SmbShare",
        "Get-NetFirewallProfile",
        "Test-NetConnection",
        "Get-ChildItem Env:",
        "BEGIN [A-Z ]*PRIVATE KEY",
    ]:
        assert needle in script, f"probe script misses {needle!r}"
    assert PROBE_PATH.read_text(encoding="utf-8") == script


async def test_run_guest_probes_executes_the_shipped_script() -> None:
    provider = WindowsSandboxProvider("probe", {"state_dir": "/tmp/unused"}, _Secrets())
    sandbox = _ProbeSandbox()
    checks = await provider.run_guest_probes(sandbox)
    assert sandbox.commands == [isolation.windows_probe_script()]
    assert checks["I6"].startswith("PASS")
    assert checks["I4"].startswith("FAIL")
