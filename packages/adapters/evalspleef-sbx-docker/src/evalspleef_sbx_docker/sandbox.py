"""Docker-backed sandbox instance (one container per trial).

Exec streams use aiodocker's hijacked TCP stream. File transfer deliberately
goes over exec tar streams instead of ``put_archive``/``get_archive``: dockerd
refuses the archive endpoints for read-only-rootfs containers (HTTP 400
"container rootfs is marked read-only") and cannot resolve tmpfs paths for
them (HTTP 404), while ``tar`` is in the Debian required package set and
present in every image this platform targets.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import posixpath
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from evalspleef.spi.errors import SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxSpec,
)

from ._tar import build_upload_tar, extract_download_tar

# Downloaded tar streams are spooled to a temp file through this callback
# buffer ceiling; member sizes are additionally bounded in _tar.
_DOWNLOAD_TAR_CAP = 1 << 30


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
        # Once truncated keep the first cap//2 bytes and the last cap//2 bytes.
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


class DockerSandbox(Sandbox):
    """One live container; safe for concurrent ``exec`` calls."""

    def __init__(self, provider: Any, sandbox_id: str, spec: SandboxSpec) -> None:
        self._provider = provider
        self.id = sandbox_id
        self.spec = spec
        self._timeout_tool: bool | None = None
        self._destroyed = False

    def _container(self) -> Any:
        return self._provider.client().containers.container(self.id)

    def _check_live(self) -> None:
        if self._destroyed:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "destroyed"})

    async def _has_timeout_tool(self) -> bool:
        if self._timeout_tool is None:
            probe = await self._exec_raw(
                ["/bin/sh", "-c", "command -v timeout >/dev/null 2>&1"],
                cwd=None,
                env=None,
                user=None,
                stdin=None,
                on_output=None,
                max_output_bytes=1024,
            )
            self._timeout_tool = probe.exit_code == 0
        return self._timeout_tool

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
        argv = ["/bin/sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        wrapped = False
        if timeout_s is not None and timeout_s > 0 and await self._has_timeout_tool():
            argv = [
                "timeout",
                "--kill-after=5s",
                f"{math.ceil(timeout_s * 1000) / 1000}s",
                *argv,
            ]
            wrapped = True
        return await self._exec_raw(
            argv,
            cwd=cwd,
            env=env,
            user=user,
            stdin=stdin,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            timeout_s=timeout_s,
            wrapped_timeout=wrapped,
        )

    async def _exec_raw(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        user: str | None,
        stdin: bytes | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
        timeout_s: float | None = None,
        wrapped_timeout: bool = False,
    ) -> ExecResult:
        environment = [f"{key}={value}" for key, value in (env or {}).items()]
        try:
            exec_ = await self._container().exec(
                argv,
                user=user or "",
                environment=environment,
                workdir=cwd,
                stdin=stdin is not None,
            )
        except Exception as exc:
            raise self._provider.map_error(exc, self.id) from exc

        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)

        def callback(channel: int, data: bytes) -> None:
            if on_output is not None:
                on_output("stdout" if channel == 1 else "stderr", data)

        started = time.monotonic()
        timed_out = False
        try:
            # The timeout binary enforces the deadline inside the sandbox; the
            # client-side watchdog only covers a hung daemon connection.
            watchdog = (timeout_s + 15.0) if timeout_s is not None else None
            await asyncio.wait_for(
                self._pump(exec_, stdout, stderr, stdin, callback),
                timeout=watchdog,
            )
        except TimeoutError:
            timed_out = True
            await self._kill_best_effort(argv)
        duration_s = time.monotonic() - started

        exit_code = await self._exit_code(exec_)
        if wrapped_timeout and exit_code == TIMEOUT_EXIT_CODE:
            timed_out = True
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=stdout.truncated or stderr.truncated,
        )

    async def _pump(
        self,
        exec_: Any,
        stdout: _CappedBuffer,
        stderr: _CappedBuffer,
        stdin: bytes | None,
        callback: Any,
    ) -> None:
        stream = exec_.start()
        try:
            if stdin is not None:
                await stream.write_in(stdin)
                # Half-close: signal EOF on stdin while keeping the connection
                # open for the command's output (Stream.close would drop it).
                connection = stream._resp.connection  # aiodocker Stream internals
                transport = connection.transport
                if transport is not None and transport.can_write_eof():
                    transport.write_eof()
            while True:
                message = await stream.read_out()
                if message is None:
                    break
                channel, data = message
                if channel == 1:
                    stdout.append(data)
                elif channel == 2:
                    stderr.append(data)
                else:
                    continue
                callback(channel, data)
        finally:
            await stream.close()

    async def _exit_code(self, exec_: Any) -> int:
        # After the hijacked stream hits EOF the exit code is available
        # immediately; poll briefly for slow daemon projections.
        for _ in range(20):
            info = await exec_.inspect()
            raw = info.get("ExitCode")
            if raw is not None:
                return int(raw)
            await asyncio.sleep(0.05)
        raise SandboxLost(
            detail={"sandbox_id": self.id, "reason": "exec exit code never materialised"},
        )

    async def _kill_best_effort(self, argv: list[str]) -> None:
        """Last-resort kill when the client-side watchdog fired.

        The primary timeout path is the ``timeout`` wrapper, so this only runs on
        images without coreutils; it kills every process whose command line
        contains the wrapped argv, which is best-effort by design.
        """
        needle = argv[-1][:48] if argv else ""
        if not needle:
            return
        script = (
            "for p in /proc/[0-9]*; do "
            'if grep -qa "$1" "$p/cmdline" 2>/dev/null; then '
            'kill -9 "$(basename "$p")" 2>/dev/null; fi; done'
        )
        with contextlib.suppress(Exception):
            await self._exec_raw(
                ["/bin/sh", "-c", script, "sh", needle],
                cwd=None,
                env=None,
                user=None,
                stdin=None,
                on_output=None,
                max_output_bytes=1024,
            )

    # -- file transfer over exec tar streams -----------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        # A directory is unpacked into the remote path itself, never over it: tar would
        # otherwise try to change the owner, mode and times of an existing mount point.
        into_dir = await asyncio.to_thread(lambda: local.is_dir() and not local.is_symlink())
        dest_dir = str(remote_path) if into_dir else posixpath.dirname(str(remote_path)) or "/"
        try:
            tar_bytes = build_upload_tar(local, arcname=remote_path.name, contents_only=into_dir)
        except TransferError:
            raise
        except Exception as exc:
            raise TransferError(f"cannot pack {local}: {exc}") from exc
        if dest_dir != "/":
            made = await self.exec(["mkdir", "-p", dest_dir])
            if made.exit_code != 0:
                raise TransferError(f"cannot create remote directory {dest_dir!r}: {made.stderr!r}")
        result = await self._exec_raw(
            ["tar", "-xf", "-", "-C", dest_dir],
            cwd=None,
            env=None,
            user=None,
            stdin=tar_bytes,
            on_output=None,
            max_output_bytes=1024,
        )
        if result.exit_code != 0:
            raise TransferError(f"upload to {remote!r} failed: {result.stderr!r}")

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        remote_dir = posixpath.dirname(str(remote_path)) or "/"
        cap = _DOWNLOAD_TAR_CAP if max_bytes is None else max_bytes + (1 << 20)
        with tempfile.TemporaryFile(prefix="spleef-download-") as spool:
            written = 0

            def sink(channel: str, data: bytes) -> None:
                nonlocal written
                if channel != "stdout":
                    return
                written += len(data)
                if written > cap:
                    raise TransferError(f"download of {remote!r} exceeds the size cap")
                spool.write(data)

            result = await self._exec_raw(
                ["tar", "-cf", "-", "-C", remote_dir, remote_path.name],
                cwd=None,
                env=None,
                user=None,
                stdin=None,
                on_output=sink,
                max_output_bytes=1 << 20,
            )
            if result.exit_code != 0:
                raise TransferError(
                    f"download of {remote!r} failed (exit {result.exit_code}): {result.stderr!r}"
                )
            spool.seek(0)
            try:
                # The spool is a real file, so random-access tar reading works.
                with tarfile.open(fileobj=spool, mode="r:") as archive:
                    extract_download_tar(archive, local, max_bytes=max_bytes)
            except TransferError:
                raise
            except Exception as exc:
                raise TransferError(f"failed to unpack {remote!r}: {exc}") from exc

    # -- lifecycle --------------------------------------------------------------

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._provider.release_sandbox(self)

    async def describe(self) -> dict[str, Any]:
        try:
            info = await self._container().show()
        except Exception:
            return {}
        config = info.get("Config") or {}
        return {
            "image_digest": info.get("Image") or "",
            "user": config.get("User") or self.spec.run_as_user,
            "host": "local-docker",
        }
