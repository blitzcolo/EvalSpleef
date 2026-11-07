"""PowerShell access to the Windows host for the Hyper-V provider.

The provider never talks to Hyper-V through a vendor SDK: every host operation
is a PowerShell script executed through a ``HostShell``. The real
implementation spawns ``powershell.exe -NoProfile -NonInteractive -Command``;
tests and the conformance target inject a fake shell that records the scripts
and simulates VM state, so the provider code under test is the real thing.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Protocol

from evalspleef.spi.errors import SpleefError


@dataclass(frozen=True)
class HostResult:
    """The captured result of one PowerShell script on the host."""

    exit_code: int
    stdout: str
    stderr: str


class HostShell(Protocol):
    """One synchronous PowerShell invocation interface.

    Implementations run ``script`` to completion (or kill it past ``timeout_s``)
    and return the exit code with the decoded stdout/stderr streams.
    """

    async def run(self, script: str, *, timeout_s: float) -> HostResult: ...


class PowerShellHostShell:
    """Runs scripts through ``powershell.exe`` on the local Windows host.

    The process is spawned per call so a hung Hyper-V cmdlet can never wedge the
    provider: past ``timeout_s`` the whole process tree is killed and a
    ``SpleefError`` is raised. The default timeout lives in the provider config.
    """

    def __init__(
        self,
        powershell_bin: str = "powershell.exe",
        default_timeout_s: float = 120.0,
    ) -> None:
        self._powershell_bin = powershell_bin
        self._default_timeout_s = default_timeout_s

    async def run(self, script: str, *, timeout_s: float | None = None) -> HostResult:
        limit = self._default_timeout_s if timeout_s is None else timeout_s
        argv = [
            self._powershell_bin,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SpleefError(f"cannot start {self._powershell_bin}: {exc}") from exc
        try:
            async with asyncio.timeout(limit):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise SpleefError(
                f"host shell command timed out after {limit:g}s: {script.splitlines()[0][:120]}"
            ) from None
        return HostResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def ps_quote(value: str) -> str:
    """Single-quote ``value`` for embedding in a PowerShell script."""
    return "'" + value.replace("'", "''") + "'"
