"""Async wrapper around the ``container`` CLI (TECH-DESIGN section 7.4.4).

Every invocation is an asyncio subprocess in its own session, so a timeout can
kill the whole process tree with ``SIGKILL`` instead of leaving helpers behind.
The provider never uses a shell; argv is always a list.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class CLIResult:
    """Exit code and captured streams of one CLI invocation."""

    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class ContainerCLI:
    """Runs ``container`` subcommands on the host."""

    def __init__(self, executable: str, env: Mapping[str, str] | None = None) -> None:
        self.executable = executable
        self._env = dict(env or {})

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env)
        env.update(extra or {})
        return env

    async def run(
        self,
        args: list[str],
        *,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
        stdin: bytes | None = None,
    ) -> CLIResult:
        """Run one subcommand and capture its output.

        A timeout kills the whole process group and raises ``TimeoutError``; the
        provider decides whether that is a command timeout (exec) or a failure.
        """
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment(),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
        except TimeoutError:
            await kill_process_group(process)
            raise
        except asyncio.CancelledError:
            await kill_process_group(process)
            raise
        return CLIResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )


async def kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the CLI process tree; a process that already exited is fine."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=10.0)
    except TimeoutError:
        process.kill()
        await process.wait()
