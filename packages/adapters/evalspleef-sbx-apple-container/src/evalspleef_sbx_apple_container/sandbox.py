"""One apple/container VM-backed sandbox (TECH-DESIGN section 7.4.4).

``exec`` runs through ``container exec`` with the CLI process in its own
session; a timeout kills that whole process group and reports exit code 124.
Files travel with ``container cp`` and are re-validated locally after download.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import posixpath
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from evalspleef.spi.errors import SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxSpec,
)

from ._cli import ContainerCLI, kill_process_group
from ._transfer import validate_download

if TYPE_CHECKING:
    from .provider import AppleContainerProvider

_CHUNK = 64 * 1024
_PUMP_DRAIN_S = 10.0


def _exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


class _CappedBuffer:
    """Accumulates one output stream up to a cap, keeping head and tail on overflow."""

    def __init__(self, cap: int) -> None:
        if cap <= 0:
            raise ValueError("max_output_bytes must be positive")
        self._cap = cap
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self.total = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if not self.truncated:
            if self.total <= self._cap:
                self._head += chunk
                return
            self.truncated = True
        head_room = self._cap // 2 - len(self._head)
        if head_room > 0:
            self._head += chunk[:head_room]
            chunk = chunk[head_room:]
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_bytes += len(chunk)
        max_tail = self._cap - self._cap // 2
        while self._tail_bytes > max_tail:
            self._tail_bytes -= len(self._tail.popleft())

    def bytes(self) -> bytes:
        return bytes(self._head) + b"".join(self._tail)


class AppleContainerSandbox(Sandbox):
    """One live lightweight Linux VM; safe for concurrent ``exec`` calls."""

    def __init__(
        self,
        provider: AppleContainerProvider,
        sandbox_id: str,
        spec: SandboxSpec,
        network_env: Mapping[str, str],
    ) -> None:
        self._provider = provider
        self.id = sandbox_id
        self.spec = spec
        self._network_env = dict(network_env)
        self._destroyed = False

    @property
    def cli(self) -> ContainerCLI:
        return self._provider.cli

    def _check_live(self) -> None:
        if self._destroyed:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "destroyed"})

    # -- exec -------------------------------------------------------------------

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
        self._check_live()
        argv = ["sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        run_user = user or self.spec.run_as_user
        environment = {**self.spec.env, **self._network_env, **(env or {})}

        args = ["exec"]
        if stdin is not None:
            args.append("--interactive")
        if run_user:
            args += ["--user", run_user]
        if cwd:
            args += ["--workdir", cwd]
        for key, value in environment.items():
            args += ["--env", f"{key}={value}"]
        args += [self.id, *argv]

        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            self.cli.executable,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.cli.environment(),
            start_new_session=True,
        )
        timed_out = False
        stdout_task = asyncio.create_task(self._pump(process.stdout, stdout, "stdout", on_output))
        stderr_task = asyncio.create_task(self._pump(process.stderr, stderr, "stderr", on_output))
        feed_task = asyncio.create_task(self._feed(process, stdin)) if stdin is not None else None
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                await kill_process_group(process)
            except asyncio.CancelledError:
                await kill_process_group(process)
                raise
            await self._drain(stdout_task, stderr_task, feed_task)
        finally:
            if process.returncode is None:
                await kill_process_group(process)

        duration_s = time.monotonic() - started
        exit_code = process.returncode if process.returncode is not None else -1
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        elif exit_code != 0 and not await self._provider.container_exists(self.id):
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "container disappeared"})
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=stdout.truncated or stderr.truncated,
        )

    @staticmethod
    async def _pump(
        stream: asyncio.StreamReader | None,
        buffer: _CappedBuffer,
        channel: str,
        on_output: OutputCallback | None,
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(_CHUNK):
            buffer.append(chunk)
            if on_output is not None:
                on_output("stdout" if channel == "stdout" else "stderr", chunk)

    @staticmethod
    async def _feed(process: asyncio.subprocess.Process, stdin: bytes) -> None:
        if process.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()

    async def _drain(
        self,
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
        feed_task: asyncio.Task[None] | None,
    ) -> None:
        tasks = [stdout_task, stderr_task]
        if feed_task is not None:
            tasks.append(feed_task)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=_PUMP_DRAIN_S)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- file transfer ----------------------------------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        if not await asyncio.to_thread(local.exists):
            raise TransferError(f"local path does not exist: {local}")
        parent = posixpath.dirname(str(remote_path)) or "/"
        if parent != "/":
            made = await self.exec(["mkdir", "-p", parent])
            if made.exit_code != 0:
                raise TransferError(f"cannot create remote directory {parent!r}: {made.stderr!r}")
        result = await self.cli.run(
            ["cp", str(local), f"{self.id}:{remote}"],
            timeout_s=self._provider.transfer_timeout_s,
        )
        if result.exit_code != 0:
            raise TransferError(
                f"upload to {remote!r} failed (exit {result.exit_code}): {result.stderr_text!r}"
            )

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        local.parent.mkdir(parents=True, exist_ok=True)
        result = await self.cli.run(
            ["cp", f"{self.id}:{remote}", str(local)],
            timeout_s=self._provider.transfer_timeout_s,
        )
        if result.exit_code != 0:
            raise TransferError(
                f"download of {remote!r} failed (exit {result.exit_code}): {result.stderr_text!r}"
            )
        if not await asyncio.to_thread(_exists_or_link, local):
            raise TransferError(f"download of {remote!r} produced no local path")
        validate_download(local, max_bytes=max_bytes)

    # -- lifecycle --------------------------------------------------------------

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._provider.release_sandbox(self)

    async def describe(self) -> dict[str, Any]:
        info = await self._provider.inspect(self.id)
        if info is None:
            return {}
        configuration = info.get("configuration") or {}
        return {
            "container": self.id,
            "host": "apple-container",
            "template_digest": self.spec.template.digest,
            "image": configuration.get("image") or self.spec.template.ref,
            "cpus": configuration.get("resources", {}).get("cpus"),
            "memory_mb": math.floor(
                int(configuration.get("resources", {}).get("memoryInBytes") or 0) / (1 << 20)
            ),
        }
