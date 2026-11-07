"""The real PowerShell host shell, exercised against a stand-in ``powershell.exe`` script."""

from __future__ import annotations

from pathlib import Path

import pytest
from evalspleef_sbx_hyperv.host_shell import PowerShellHostShell, ps_quote
from evalspleef_sbx_hyperv.provider import HyperVProvider

from evalspleef.spi.errors import SpleefError


class _NoSecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


def _stand_in(directory: Path, body: str) -> str:
    """Write an executable stand-in for ``powershell.exe`` and return its path."""
    script = directory / "fake-powershell"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_ps_quote_escapes_single_quotes() -> None:
    assert ps_quote("plain") == "'plain'"
    assert ps_quote("it's") == "'it''s'"
    assert ps_quote("") == "''"


async def test_run_passes_the_script_with_noninteractive_flags(tmp_path: Path) -> None:
    binary = _stand_in(tmp_path, "printf '%s|' \"$@\"; printf 'stderr-text' >&2; exit 3")
    shell = PowerShellHostShell(powershell_bin=binary)
    result = await shell.run("Get-VM", timeout_s=30.0)
    assert result.exit_code == 3
    assert result.stdout == "-NoProfile|-NonInteractive|-Command|Get-VM|"
    assert result.stderr == "stderr-text"


async def test_run_kills_a_hung_script_at_the_deadline(tmp_path: Path) -> None:
    shell = PowerShellHostShell(powershell_bin=_stand_in(tmp_path, "exec sleep 30"))
    with pytest.raises(SpleefError, match="timed out"):
        await shell.run("Start-VM -Name 'x'", timeout_s=0.2)


async def test_run_reports_a_missing_binary(tmp_path: Path) -> None:
    shell = PowerShellHostShell(powershell_bin=str(tmp_path / "does-not-exist"))
    with pytest.raises(SpleefError, match="cannot start"):
        await shell.run("Get-VM", timeout_s=5.0)


async def test_provider_defaults_to_the_real_host_shell(tmp_path: Path) -> None:
    binary = _stand_in(tmp_path, "printf '%s|' \"$@\"")
    provider = HyperVProvider(
        "stand-in",
        {
            "powershell_bin": binary,
            "state_dir": str(tmp_path / "state"),
            "vm_dir": str(tmp_path / "vhds"),
        },
        _NoSecrets(),
    )
    report = await provider.preflight()
    assert report["powershell_version"].startswith("-NoProfile|-NonInteractive|-Command|")
    assert report["hyperv_module"].endswith("Get-Command Get-VM -ErrorAction Stop).Source|")
