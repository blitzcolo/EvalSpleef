"""One apple/container VM-backed sandbox (TECH-DESIGN section 7.4.4).

``exec`` runs through ``container exec``. Killing the CLI does not stop the
guest process (the VM keeps it running), so every command is started under a
small ``sh`` wrapper that records its pid; the guest process is a session and
process-group leader, and a timeout kills that group inside the VM before the
result (exit code 124) is returned.

Files travel as tar streams over ``exec`` stdin/stdout, run as the sandbox user; a
read-only upload is unpacked by root (``exec --user root`` works with ``--cap-drop ALL``:
root then owns what it creates but cannot chown or override permissions).
``container cp`` is not used: it reads and writes the VM's root filesystem
underneath the tmpfs mounts (and past ``--read-only``), so it neither sees
``/workspace`` nor respects the sandbox's own view of its files.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import posixpath
import secrets
import tempfile
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
from ._transfer import build_upload_tar, extract_download_tar, make_read_only

if TYPE_CHECKING:
    from .provider import AppleContainerProvider

_CHUNK = 64 * 1024
_PUMP_DRAIN_S = 10.0
_KILL_TIMEOUT_S = 15.0
_DOWNLOAD_TAR_CAP = 4 << 30
"""Upper bound on a download stream when the caller sets no ``max_bytes``."""

# Records the wrapper's pid (the guest process-group leader) in $0, runs the command,
# then removes the record. The pid file lives on the sandbox's private /tmp tmpfs.
_PID_WRAPPER = '{ echo "$$" > "$0"; } 2>/dev/null; "$@"; status=$?; rm -f "$0"; exit "$status"'
# No "--" before the negative pid: dash's kill builtin rejects it ("Illegal number: -").
_KILL_GROUP = 'pid="$(cat "$1" 2>/dev/null)"; [ -n "$pid" ] && kill -KILL "-$pid"; rm -f "$1"'
_UNPACK = 'mkdir -p "$1" && umask 000 && exec tar -xf - -C "$1"'
_UNPACK_AS_ROOT = 'exec tar --no-same-owner -xf - -C "$1"'


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
        command = ["sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        run_user = user or self.spec.run_as_user
        environment = {**self.spec.env, **self._network_env, **(env or {})}
        pid_file = f"/tmp/.exec-{secrets.token_hex(8)}.pid"
        argv = ["sh", "-c", _PID_WRAPPER, pid_file, *command]

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
                await self._kill_guest_group(pid_file, run_user)
            except asyncio.CancelledError:
                await kill_process_group(process)
                await self._kill_guest_group(pid_file, run_user)
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

    async def _kill_guest_group(self, pid_file: str, user: str) -> None:
        """Kill the timed-out command's process group inside the VM.

        A missing pid file means the command already finished; a kill that cannot
        run because the container is gone surfaces as ``SandboxLost`` on the next call.
        """
        args = ["exec", "--user", user, self.id, "sh", "-c", _KILL_GROUP, "sh", pid_file]
        try:
            await self.cli.run(args, timeout_s=_KILL_TIMEOUT_S)
        except TimeoutError:
            return

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

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        if not remote_path.name:
            raise TransferError(f"remote path must name a file or directory, got {remote!r}")
        # A directory unpacks INTO the remote path, never over it: tar would otherwise
        # set the mode and times of an existing root-owned mount point such as
        # /workspace, which the sandbox user may not do (GNU tar exits 2).
        # A read-only tree keeps its own top entry so root creates (and owns) it.
        is_dir = await asyncio.to_thread(lambda: local.is_dir() and not local.is_symlink())
        into_dir = is_dir and not read_only
        target = str(remote_path) if into_dir else posixpath.dirname(str(remote_path)) or "/"
        archive = await asyncio.to_thread(
            build_upload_tar, local, arcname=remote_path.name, contents_only=into_dir
        )
        if read_only:
            archive = await asyncio.to_thread(make_read_only, archive)
            # Parents are the sandbox user's, created like any other upload's.
            made = await self.exec(["mkdir", "-p", target], timeout_s=60.0)
            if made.exit_code != 0:
                raise TransferError(f"cannot create {target!r} for {remote!r}: {made.stderr!r}")
        result = await self.exec(
            ["sh", "-c", _UNPACK_AS_ROOT if read_only else _UNPACK, "sh", target],
            stdin=archive,
            user="root" if read_only else None,
            timeout_s=self._provider.transfer_timeout_s,
            max_output_bytes=1 << 20,
        )
        if result.exit_code != 0:
            raise TransferError(
                f"upload to {remote!r} failed (exit {result.exit_code}): {result.stderr!r}"
            )

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        if not remote_path.name:
            raise TransferError(f"remote path must name a file or directory, got {remote!r}")
        parent = posixpath.dirname(str(remote_path)) or "/"
        cap = _DOWNLOAD_TAR_CAP if max_bytes is None else max_bytes + (1 << 20)
        with tempfile.TemporaryFile(prefix="spleef-download-") as spool:
            written = 0
            over_cap = False

            def sink(channel: str, data: bytes) -> None:
                nonlocal written, over_cap
                if channel != "stdout" or over_cap:
                    return
                written += len(data)
                if written > cap:
                    over_cap = True
                    return
                spool.write(data)

            result = await self.exec(
                ["tar", "-cf", "-", "-C", parent, f"./{remote_path.name}"],
                timeout_s=self._provider.transfer_timeout_s,
                on_output=sink,
                max_output_bytes=1 << 20,
            )
            if over_cap:
                raise TransferError(f"download of {remote!r} exceeds the size cap of {cap} bytes")
            if result.exit_code != 0:
                raise TransferError(
                    f"download of {remote!r} failed (exit {result.exit_code}): {result.stderr!r}"
                )
            spool.seek(0)
            await asyncio.to_thread(extract_download_tar, spool, local, max_bytes=max_bytes)

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
        image = configuration.get("image")
        # The CLI reports the image as {"reference": ..., "descriptor": {"digest": ...}}.
        reference = image.get("reference") if isinstance(image, dict) else image
        return {
            "container": self.id,
            "host": "apple-container",
            "template_digest": self.spec.template.digest,
            "image": reference or self.spec.template.ref,
            "cpus": configuration.get("resources", {}).get("cpus"),
            "memory_mb": math.floor(
                int(configuration.get("resources", {}).get("memoryInBytes") or 0) / (1 << 20)
            ),
        }
