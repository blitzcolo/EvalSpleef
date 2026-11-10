"""One Tart VM as a sandbox instance.

Commands travel through ``tart exec`` (the guest agent) and, when configured or
when the guest agent is unreachable, through SSH via ``asyncssh``. Both
transports run the command as the unprivileged ``spec.run_as_user`` account via
``sudo -u <user> -i`` from the management account that owns the VM. Output is
streamed and capped; on timeout the whole process group is killed and the
result reports exit code 124.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import shlex
import signal
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxSpec,
)

from ._tar import build_upload_tar, extract_download_tar

if TYPE_CHECKING:
    from .provider import TartProvider

WORKSPACE = "/workspace"
_DOWNLOAD_TAR_CAP = 1 << 30
_TRANSPORT_FAILURE_MARKERS = (
    b"guest agent",
    b"not running",
    b"failed to connect",
    b"connection refused",
)
_SshFactory = Callable[..., Awaitable[Any]]


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


class _RawResult:
    def __init__(
        self,
        exit_code: int,
        stdout: _CappedBuffer,
        stderr: _CappedBuffer,
        timed_out: bool,
        duration_s: float,
        transport: str,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_s = duration_s
        self.transport = transport


class TartSandbox(Sandbox):
    """One live Tart VM; safe for concurrent ``exec`` calls."""

    def __init__(self, provider: TartProvider, vm_name: str, spec: SandboxSpec) -> None:
        self._provider = provider
        self.id = vm_name
        self.spec = spec
        self._destroyed = False
        self._ssh_transport = provider.exec_transport == "ssh"

    def _check_live(self) -> None:
        if self._destroyed or not self._provider.is_live(self.id):
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "not live"})

    def _remote_argv(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        user: str | None,
    ) -> list[str]:
        inner = ["/bin/sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        if env:
            inner = ["env", *(f"{key}={value}" for key, value in env.items()), *inner]
        workdir = cwd or WORKSPACE
        script = f"cd -- {shlex.quote(workdir)} && exec {shlex.join(inner)}"
        account = user or self.spec.run_as_user
        return ["sudo", "-n", "-u", account, "-i", "--", "/bin/sh", "-lc", script]

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
        remote = self._remote_argv(cmd, cwd=cwd, env=env, user=user)
        if self._ssh_transport:
            raw = await self._run_ssh(
                remote,
                stdin=stdin,
                timeout_s=timeout_s,
                on_output=on_output,
                max_output_bytes=max_output_bytes,
            )
        else:
            raw = await self._run_guest_agent(
                remote,
                stdin=stdin,
                timeout_s=timeout_s,
                on_output=on_output,
                max_output_bytes=max_output_bytes,
            )
            if (
                self._provider.exec_transport == "auto"
                and raw.exit_code != 0
                and not raw.stdout.total
                and _is_transport_failure(raw.stderr.bytes())
            ):
                self._ssh_transport = True
                raw = await self._run_ssh(
                    remote,
                    stdin=stdin,
                    timeout_s=timeout_s,
                    on_output=on_output,
                    max_output_bytes=max_output_bytes,
                )
        return ExecResult(
            exit_code=raw.exit_code,
            stdout=raw.stdout.bytes(),
            stderr=raw.stderr.bytes(),
            timed_out=raw.timed_out,
            duration_s=raw.duration_s,
            truncated=raw.stdout.truncated or raw.stderr.truncated,
        )

    # -- guest agent transport -------------------------------------------------

    async def _run_guest_agent(
        self,
        remote: list[str],
        *,
        stdin: bytes | None,
        timeout_s: float | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> _RawResult:
        argv = [*self._provider.tart_argv("exec", self.id, "--"), *remote]
        return await self._run_process(
            argv,
            stdin=stdin,
            timeout_s=timeout_s,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            transport="guest-agent",
        )

    # -- SSH transport ---------------------------------------------------------

    async def _run_ssh(
        self,
        remote: list[str],
        *,
        stdin: bytes | None,
        timeout_s: float | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> _RawResult:
        connector: _SshFactory = self._provider.ssh_connector
        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)
        started = time.monotonic()
        try:
            connection = await connector(await self._provider.ssh_target(self.id))
            process = await connection.create_process(shlex.join(remote), encoding=None)
        except Exception as exc:
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": f"ssh connect failed: {exc}"}
            ) from exc
        try:
            if stdin is not None and process.stdin is not None:
                process.stdin.write(stdin)
                process.stdin.write_eof()
            reads = [
                asyncio.create_task(read(process.stdout, stdout, "stdout", on_output)),
                asyncio.create_task(read(process.stderr, stderr, "stderr", on_output)),
            ]
            timed_out = False
            try:
                waiting: list[Awaitable[Any]] = [*reads, process.wait()]
                if timeout_s is None:
                    await asyncio.gather(*waiting)
                else:
                    await asyncio.wait_for(asyncio.gather(*waiting), timeout=timeout_s)
                exit_code = _ssh_exit_code(await process.wait())
            except TimeoutError:
                timed_out = True
                with contextlib.suppress(Exception):
                    process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=15.0)
                await asyncio.gather(*reads, return_exceptions=True)
                exit_code = TIMEOUT_EXIT_CODE
        finally:
            with contextlib.suppress(Exception):
                connection.close()
                await connection.wait_closed()
        return _RawResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=time.monotonic() - started,
            transport="ssh",
        )

    # -- local process runner --------------------------------------------------

    async def _run_process(
        self,
        argv: list[str],
        *,
        stdin: bytes | None,
        timeout_s: float | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
        transport: str,
    ) -> _RawResult:
        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": f"cannot start tart: {exc}"}
            ) from exc

        async def read(
            stream: asyncio.StreamReader,
            buffer: _CappedBuffer,
            channel: Literal["stdout", "stderr"],
        ) -> None:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buffer.append(chunk)
                if on_output is not None:
                    on_output(channel, chunk)

        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(read(process.stdout, stdout, "stdout")),
            asyncio.create_task(read(process.stderr, stderr, "stderr")),
        ]
        if stdin is not None and process.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(stdin)
                await process.stdin.drain()
                process.stdin.close()

        timed_out = False
        try:
            if timeout_s is None:
                await process.wait()
            else:
                await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            self._kill_tree(process)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=15.0)
        await asyncio.gather(*readers, return_exceptions=True)

        exit_code = TIMEOUT_EXIT_CODE if timed_out else int(process.returncode or 0)
        return _RawResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=time.monotonic() - started,
            transport=transport,
        )

    @staticmethod
    def _kill_tree(process: asyncio.subprocess.Process) -> None:
        """Kill the whole session started for ``tart exec`` (tart and its children)."""
        with contextlib.suppress(ProcessLookupError):
            os_killpg(process.pid, signal.SIGKILL)

    # -- file transfer over exec tar streams -----------------------------------

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if read_only:
            raise CapabilityNotSupported("read_only_upload")
        self._check_live()
        remote_path = PurePosixPath(remote)
        if not remote_path.is_absolute():
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        # A directory is unpacked into the remote path itself, never over it: tar would
        # otherwise try to recreate or re-mode an existing mount point such as /workspace.
        into_dir = await asyncio.to_thread(lambda: local.is_dir() and not local.is_symlink())
        dest_dir = str(remote_path) if into_dir else posixpath.dirname(str(remote_path)) or "/"
        try:
            tar_bytes = build_upload_tar(local, arcname=remote_path.name, contents_only=into_dir)
        except TransferError:
            raise
        except Exception as exc:
            raise TransferError(f"cannot pack {local}: {exc}") from exc
        made = await self.exec(["mkdir", "-p", dest_dir])
        if made.exit_code != 0:
            raise TransferError(f"cannot create remote directory {dest_dir!r}: {made.stderr!r}")
        result = await self.exec(
            ["tar", "-xf", "-", "-C", dest_dir],
            stdin=tar_bytes,
            max_output_bytes=1 << 20,
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
        with tempfile.TemporaryFile(prefix="spleef-tart-download-") as spool:
            written = 0

            def sink(channel: str, data: bytes) -> None:
                nonlocal written
                if channel != "stdout":
                    return
                written += len(data)
                if written > cap:
                    raise TransferError(f"download of {remote!r} exceeds the size cap")
                spool.write(data)

            result = await self.exec(
                ["tar", "-cf", "-", "-C", remote_dir, remote_path.name],
                on_output=sink,
                max_output_bytes=1 << 20,
            )
            if result.exit_code != 0:
                raise TransferError(
                    f"download of {remote!r} failed (exit {result.exit_code}): {result.stderr!r}"
                )
            spool.seek(0)
            try:
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
        await self._provider.release(self.id)

    async def gui_endpoint(self) -> str:
        if not self.spec.gui:
            raise CapabilityNotSupported("gui not requested for this sandbox")
        return await self._provider.vnc_endpoint(self.id)

    async def describe(self) -> dict[str, Any]:
        return await self._provider.describe_vm(self.id)


def _is_transport_failure(stderr: bytes) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _TRANSPORT_FAILURE_MARKERS)


async def read(
    stream: Any,
    buffer: _CappedBuffer,
    channel: Literal["stdout", "stderr"],
    on_output: OutputCallback | None,
) -> None:
    """Drain one SSH stream into ``buffer`` (and the callback) until EOF."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buffer.append(chunk)
        if on_output is not None:
            on_output(channel, chunk)


def _ssh_exit_code(result: Any) -> int:
    code = getattr(result, "exit_status", None)
    if code is None:
        code = getattr(result, "returncode", 0)
    return int(code or 0)


def os_killpg(pid: int, sig: int) -> None:
    """``os.killpg`` with a local import so the module stays importable on Windows."""
    import os

    os.killpg(pid, sig)
