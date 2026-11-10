"""One Hyper-V VM as a sandbox instance.

Commands travel over the guest's OpenSSH port through asyncssh. A ``str``
command runs under the OS default shell (``powershell -NoProfile
-NonInteractive -Command`` on Windows guests, ``/bin/sh -lc`` on Linux
guests); a ``list`` command is joined with the platform's quoting rules and
never re-parsed by a shell. Output streams incrementally with a head-and-tail
cap; on timeout the whole remote process tree is killed (``taskkill /T /F``
against a pid file the command records, or a process-group kill on Linux) and
the result reports exit code 124.

Files move over SFTP: plain files directly, directories as zip archives
(``Compress-Archive``/``Expand-Archive``) on Windows guests or tar streams on
Linux guests, with the I11 sanitisation on download. When the guest cannot be
reached over the network at all, the provider can fall back to PowerShell
Direct ``Copy-Item -ToSession/-FromSession`` (config ``transfer: ps-direct``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import posixpath
import re
import shlex
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Mapping
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
from ._zip import build_upload_zip, extract_download_zip

if TYPE_CHECKING:
    import asyncssh

    from .provider import HyperVProvider

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DOWNLOAD_CAP = 1 << 30


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


class HyperVSandbox(Sandbox):
    """One live Hyper-V VM; safe for concurrent ``exec`` calls."""

    def __init__(self, provider: HyperVProvider, vm_name: str, spec: SandboxSpec) -> None:
        self._provider = provider
        self.id = vm_name
        self.spec = spec
        self._destroyed = False
        self._ssh: asyncssh.SSHClientConnection | None = None
        self._ssh_lock = asyncio.Lock()

    # -- live checks and SSH connection ------------------------------------------

    def _check_live(self) -> None:
        if self._destroyed or not self._provider.is_live(self.id):
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "not live"})

    async def _get_ssh(self) -> asyncssh.SSHClientConnection:
        self._check_live()
        async with self._ssh_lock:
            if self._ssh is not None:
                return self._ssh
            host, port = await self._provider.guest_address(self.id)
            try:
                self._ssh = await _connect_ssh(self._provider, host, port)
            except Exception as exc:
                raise SandboxLost(
                    detail={"sandbox_id": self.id, "reason": f"ssh connect failed: {exc}"}
                ) from exc
            return self._ssh

    async def _drop_ssh(self) -> None:
        async with self._ssh_lock:
            connection = self._ssh
            self._ssh = None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
            with contextlib.suppress(Exception):
                await connection.wait_closed()

    # -- remote command construction ----------------------------------------------

    def _remote_command(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout_s: float | None,
    ) -> tuple[str, str | None]:
        """Build ``(remote_command_line, guest_pid_file | None)``.

        The pid file is recorded by the command itself only when a timeout was
        requested; it lets the timeout path kill the exact process tree later.
        """
        pidfile: str | None = None
        if self.spec.os.value == "windows":
            if timeout_s is not None:
                pidfile = f"C:/Windows/Temp/spleef-{uuid.uuid4().hex}.pid"
            needs_wrapper = pidfile is not None or cwd is not None or bool(env)
            if isinstance(cmd, str):
                script = cmd
            elif needs_wrapper:
                script = "& " + " ".join(_ps_arg(arg) for arg in cmd)
            else:
                return " ".join(_win_arg(arg) for arg in cmd), None
            prefix = "".join(
                f"$env:{key} = {_ps_arg(value)}; " for key, value in (env or {}).items()
            )
            if cwd is not None:
                prefix += f"Set-Location -LiteralPath {_ps_arg(cwd)}; "
            if pidfile is not None:
                prefix += f"$pid | Out-File -Encoding ascii -FilePath {_ps_arg(pidfile)}; "
            encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode("ascii")
            return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}", pidfile
        # Linux guest
        if timeout_s is not None:
            pidfile = f"/tmp/spleef-{uuid.uuid4().hex}.pid"
        inner = f"sh -lc {shlex.quote(cmd)}" if isinstance(cmd, str) else shlex.join(list(cmd))
        steps: list[str] = []
        if pidfile is not None:
            steps.append(f"echo $$ > {shlex.quote(pidfile)}")
        if cwd is not None:
            steps.append(f"cd {shlex.quote(cwd)}")
        steps.extend(f"export {key}={shlex.quote(value)}" for key, value in (env or {}).items())
        if not steps:
            return inner, None
        steps.append(f"exec {inner}")
        return f"sh -c {shlex.quote(' && '.join(steps))}", pidfile

    # -- exec ------------------------------------------------------------------------

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
        if user is not None and user != self.spec.run_as_user:
            raise CapabilityNotSupported(
                f"hyperv guests run commands as the SSH account "
                f"{self.spec.run_as_user!r}; switching to {user!r} is not supported"
            )
        remote, pidfile = self._remote_command(cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        return await self._run_remote(
            remote,
            pidfile=pidfile,
            stdin=stdin,
            timeout_s=timeout_s,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
        )

    async def _run_remote(
        self,
        remote: str,
        *,
        pidfile: str | None,
        stdin: bytes | None,
        timeout_s: float | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> ExecResult:
        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)
        started = time.monotonic()
        connection = await self._get_ssh()
        try:
            process = await connection.create_process(remote, encoding=None)
        except Exception as exc:
            await self._drop_ssh()
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": f"ssh exec failed: {exc}"}
            ) from exc
        if process.stdin is not None:
            if stdin:
                process.stdin.write(stdin)
            process.stdin.write_eof()
        readers = [
            asyncio.create_task(_drain(process.stdout, stdout, "stdout", on_output)),
            asyncio.create_task(_drain(process.stderr, stderr, "stderr", on_output)),
        ]
        timed_out = False
        completed: Any = None
        try:
            if timeout_s is None:
                completed = await process.wait()
            else:
                completed = await asyncio.wait_for(process.wait(), timeout=timeout_s)
            await asyncio.gather(*readers)
        except TimeoutError:
            timed_out = True
            await self._kill_tree(pidfile, process)
            with contextlib.suppress(Exception):
                completed = await asyncio.wait_for(process.wait(), timeout=15.0)
            await asyncio.gather(*readers, return_exceptions=True)
        duration_s = time.monotonic() - started
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        else:
            raw = getattr(completed, "exit_status", None)
            if raw is None:
                raw = getattr(completed, "returncode", None)
            exit_code = int(raw) if raw is not None else 0
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=stdout.truncated or stderr.truncated,
        )

    async def _kill_tree(
        self,
        pidfile: str | None,
        process: Any,
    ) -> None:
        """Kill the whole remote process tree, then drop the timed-out channel.

        The tree kill goes over a fresh channel so it still runs when the
        original one is wedged; failures are best-effort because closing the
        SSH channel makes the server terminate the direct child as well.
        """
        if pidfile is not None:
            with contextlib.suppress(Exception):
                pid = (await self._read_pidfile(pidfile)).strip()
                if pid.isdigit():
                    if self.spec.os.value == "windows":
                        await self._run_simple(f"taskkill /T /F /PID {pid}")
                    else:
                        # Walk the children of the recorded shell (needs pgrep in the image).
                        await self._run_simple(
                            'kill_tree() { for c in $(pgrep -P "$1" 2>/dev/null); '
                            'do kill_tree "$c"; done; kill -KILL "$1" 2>/dev/null; }; '
                            f"kill_tree {pid}"
                        )
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10.0)

    async def _read_pidfile(self, pidfile: str) -> str:
        if self.spec.os.value == "windows":
            result = await self._run_simple(f"cmd /c type {pidfile}")
        else:
            result = await self._run_simple(f"cat {shlex.quote(pidfile)}")
        if result.exit_code != 0:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": f"cannot read {pidfile!r}"})
        return result.stdout.decode("utf-8", errors="replace")

    async def _run_simple(self, remote: str) -> ExecResult:
        """Run one short command to completion (no timeout, small buffers)."""
        return await self._run_remote(
            remote,
            pidfile=None,
            stdin=None,
            timeout_s=None,
            on_output=None,
            max_output_bytes=1 << 20,
        )

    # -- file transfer -----------------------------------------------------------------

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if read_only:
            raise CapabilityNotSupported("read_only_upload")
        self._check_live()
        remote = _check_remote(remote)
        if self._provider_transfer() == "ps-direct":
            await self._upload_ps_direct(local, remote)
            return
        if self.spec.os.value == "windows":
            await self._upload_windows(local, remote)
        else:
            await self._upload_linux(local, remote)

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        remote = _check_remote(remote)
        if self._provider_transfer() == "ps-direct":
            await self._download_ps_direct(remote, local, max_bytes=max_bytes)
            return
        connection = await self._get_ssh()
        sftp = await connection.start_sftp_client()
        await self._reject_escaping_symlink(sftp, remote)
        if await _sftp_isdir(sftp, remote):
            if self.spec.os.value == "windows":
                await self._download_windows_dir(sftp, remote, local, max_bytes=max_bytes)
            else:
                await self._download_linux_dir(remote, local, max_bytes=max_bytes)
        else:
            attrs = await self._sftp_stat(sftp, remote)
            if max_bytes is not None and int(attrs.size or 0) > max_bytes:
                raise TransferError(
                    f"download of {remote!r} exceeds max_bytes={max_bytes} "
                    f"(file is {int(attrs.size or 0)} bytes)"
                )
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                await sftp.get(remote, str(local), follow_symlinks=True)
            except Exception as exc:
                raise TransferError(f"download of {remote!r} failed: {exc}") from exc

    def _provider_transfer(self) -> str:
        return self._provider.transfer

    async def _reject_escaping_symlink(self, sftp: Any, remote: str) -> None:
        """Refuse to download a symlink whose target leaves the workspace (I11)."""
        try:
            attrs = await sftp.lstat(remote)
        except Exception:
            return  # not present or not a link; the transfer itself reports the error
        if not stat.S_ISLNK(int(attrs.permissions or 0)):
            return
        try:
            target = await sftp.readlink(remote)
        except Exception as exc:
            raise TransferError(f"cannot inspect symlink {remote!r}: {exc}") from exc
        pure = PurePosixPath(target)
        if pure.is_absolute() or _WINDOWS_ABS_RE.match(target) is not None or ".." in pure.parts:
            raise TransferError(
                f"refusing to download symlink escaping the sandbox: {remote!r} -> {target!r}"
            )

    async def _sftp_stat(self, sftp: Any, remote: str) -> Any:
        try:
            return await sftp.stat(remote)
        except Exception as exc:
            raise TransferError(f"cannot stat {remote!r}: {exc}") from exc

    async def _upload_windows(self, local: Path, remote: str) -> None:
        parent = posixpath.dirname(remote)
        await self._ensure_remote_dir(parent)
        connection = await self._get_ssh()
        sftp = await connection.start_sftp_client()
        if await asyncio.to_thread(local.is_dir):
            data = build_upload_zip(local, arcname=PurePosixPath(remote).name)
            tmp_remote = posixpath.join(parent, f".spleef-{uuid.uuid4().hex}.zip")
            with tempfile.NamedTemporaryFile(prefix="spleef-upload-", suffix=".zip") as tmp:
                tmp.write(data)
                tmp.flush()
                await sftp.put(tmp.name, tmp_remote)
            expanded = await self._run_simple(
                _powershell_script(
                    f"Expand-Archive -LiteralPath {_ps_arg(tmp_remote)} "
                    f"-DestinationPath {_ps_arg(parent)} -Force; "
                    f"Remove-Item -LiteralPath {_ps_arg(tmp_remote)} -Force"
                )
            )
            if expanded.exit_code != 0:
                raise TransferError(f"upload to {remote!r} failed: {expanded.stderr!r}")
        else:
            try:
                await sftp.put(str(local), remote, follow_symlinks=True)
            except Exception as exc:
                raise TransferError(f"upload to {remote!r} failed: {exc}") from exc

    async def _download_windows_dir(
        self,
        sftp: Any,
        remote: str,
        local: Path,
        *,
        max_bytes: int | None,
    ) -> None:
        tmp_remote = posixpath.join(posixpath.dirname(remote), f".spleef-{uuid.uuid4().hex}.zip")
        packed = await self._run_simple(
            _powershell_script(
                f"Compress-Archive -Path {_ps_arg(remote)} -DestinationPath {_ps_arg(tmp_remote)} "
                "-Force"
            )
        )
        if packed.exit_code != 0:
            raise TransferError(f"download of {remote!r} failed: {packed.stderr!r}")
        with tempfile.NamedTemporaryFile(prefix="spleef-download-", suffix=".zip") as tmp:
            try:
                await sftp.get(tmp_remote, tmp.name)
            except Exception as exc:
                raise TransferError(f"download of {remote!r} failed: {exc}") from exc
            finally:
                with contextlib.suppress(Exception):
                    await self._run_simple(
                        _powershell_script(f"Remove-Item -LiteralPath {_ps_arg(tmp_remote)} -Force")
                    )
            tmp.seek(0)
            try:
                with zipfile.ZipFile(tmp) as archive:
                    extract_download_zip(archive, local, max_bytes=max_bytes)
            except TransferError:
                raise
            except Exception as exc:
                raise TransferError(f"failed to unpack {remote!r}: {exc}") from exc

    async def _upload_linux(self, local: Path, remote: str) -> None:
        parent = posixpath.dirname(remote)
        await self._ensure_remote_dir(parent)
        if await asyncio.to_thread(local.is_dir):
            data = build_upload_tar(local, arcname=PurePosixPath(remote).name)
            result = await self._run_remote(
                f"tar -xf - -C {shlex.quote(parent)}",
                pidfile=None,
                stdin=data,
                timeout_s=None,
                on_output=None,
                max_output_bytes=1 << 20,
            )
            if result.exit_code != 0:
                raise TransferError(f"upload to {remote!r} failed: {result.stderr!r}")
        else:
            connection = await self._get_ssh()
            sftp = await connection.start_sftp_client()
            try:
                await sftp.put(str(local), remote, follow_symlinks=True)
            except Exception as exc:
                raise TransferError(f"upload to {remote!r} failed: {exc}") from exc

    async def _download_linux_dir(self, remote: str, local: Path, *, max_bytes: int | None) -> None:
        parent = posixpath.dirname(remote) or "/"
        name = PurePosixPath(remote).name
        cap = _DOWNLOAD_CAP if max_bytes is None else max_bytes + (1 << 20)
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

            result = await self._run_remote(
                f"tar -cf - -C {shlex.quote(parent)} {shlex.quote(name)}",
                pidfile=None,
                stdin=None,
                timeout_s=None,
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

    # -- PowerShell Direct fallback -------------------------------------------------------

    async def _upload_ps_direct(self, local: Path, remote: str) -> None:
        if not await asyncio.to_thread(local.exists):
            raise TransferError(f"local path does not exist: {local}")
        await self._provider.ps_direct_copy(
            self.id,
            source=str(local),
            destination=_guest_native_path(remote, self.spec.os.value),
            to_guest=True,
        )

    async def _download_ps_direct(self, remote: str, local: Path, *, max_bytes: int | None) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        await self._provider.ps_direct_copy(
            self.id,
            source=_guest_native_path(remote, self.spec.os.value),
            destination=str(local),
            to_guest=False,
        )
        problem = await asyncio.to_thread(_inspect_downloaded_tree, local, max_bytes)
        if problem is not None:
            await asyncio.to_thread(_discard_download, local)
            raise TransferError(f"refusing download of {remote!r}: {problem}")

    async def _ensure_remote_dir(self, parent: str) -> None:
        if not parent or parent == "/":
            return
        if self.spec.os.value == "windows":
            made = await self._run_simple(f"cmd /c mkdir {parent}")
        else:
            made = await self._run_simple(f"mkdir -p {shlex.quote(parent)}")
        if made.exit_code != 0:
            raise TransferError(f"cannot create remote directory {parent!r}: {made.stderr!r}")

    # -- lifecycle ------------------------------------------------------------------------

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._drop_ssh()
        await self._provider.release(self.id)

    async def snapshot(self, name: str) -> str:
        self._check_live()
        return await self._provider.checkpoint(self.id, name)

    async def gui_endpoint(self) -> str:
        if not self.spec.gui:
            raise CapabilityNotSupported("gui not requested for this sandbox")
        return await self._provider.rdp_endpoint(self.id)

    async def describe(self) -> dict[str, Any]:
        return await self._provider.describe_vm(self.id)


async def _drain(
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


def _ps_arg(value: str) -> str:
    """Single-quote an argument for a PowerShell command line."""
    return "'" + value.replace("'", "''") + "'"


def _win_arg(value: str) -> str:
    """Quote one argument per the Windows CommandLineToArgvW rules."""
    if value == "":
        return '""'
    if not re.search(r'[\s"&|<>^%!()]', value):
        return value
    parts = ['"']
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
        elif char == '"':
            parts.append("\\" * (backslashes * 2 + 1) + '"')
            backslashes = 0
        else:
            parts.append("\\" * backslashes + char)
            backslashes = 0
    parts.append("\\" * (backslashes * 2) + '"')
    return "".join(parts)


def _powershell_script(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _check_remote(remote: str) -> str:
    pure = PurePosixPath(remote)
    if not remote.startswith("/") and _WINDOWS_ABS_RE.match(remote) is None:
        raise TransferError(f"remote path must be absolute, got {remote!r}")
    if ".." in pure.parts:
        raise TransferError(f"remote path escapes the workspace: {remote!r}")
    return remote


def _guest_native_path(remote: str, os_value: str) -> str:
    """PowerShell Direct takes native paths: ``/workspace/x`` is ``C:\\workspace\\x`` on Windows."""
    if os_value == "windows" and remote.startswith("/"):
        return "C:" + remote.replace("/", "\\")
    return remote


def _discard_download(local: Path) -> None:
    if local.is_dir() and not local.is_symlink():
        shutil.rmtree(local, ignore_errors=True)
    else:
        local.unlink(missing_ok=True)


def _inspect_downloaded_tree(local: Path, max_bytes: int | None) -> str | None:
    """Post-copy I11 check for the PowerShell Direct path: no links, no oversize."""
    total = 0
    for path in [local, *local.rglob("*")] if local.is_dir() else [local]:
        if path.is_symlink():
            return f"symlink {path.name!r} in the downloaded tree"
        if path.is_file():
            total += path.stat().st_size
    if max_bytes is not None and total > max_bytes:
        return f"download is {total} bytes, max_bytes={max_bytes}"
    return None


async def _sftp_isdir(sftp: Any, remote: str) -> bool:
    try:
        attrs = await sftp.stat(remote)
    except Exception as exc:
        raise TransferError(f"cannot stat {remote!r}: {exc}") from exc
    return stat.S_ISDIR(int(attrs.permissions or 0))


async def _connect_ssh(provider: HyperVProvider, host: str, port: int) -> Any:
    """Open one SSH connection to the guest with the configured credentials."""
    import asyncssh

    kwargs: dict[str, Any] = {
        "port": port,
        "username": provider.ssh_user,
        "known_hosts": provider.ssh_known_hosts,
        "login_timeout": provider.ssh_connect_timeout_s,
    }
    if provider.ssh_key is not None:
        kwargs["client_keys"] = [provider.secrets.resolve(provider.ssh_key)]
    if provider.ssh_password is not None:
        kwargs["password"] = provider.secrets.resolve(provider.ssh_password)
    return await asyncssh.connect(host, **kwargs)
