"""Libvirt-backed sandbox instance (one QEMU/KVM domain per trial).

Command execution goes over SSH with asyncssh so output streams and exit codes
survive; the qemu-guest-agent ``guest-exec`` path is a documented fallback for
when SSH is unavailable and delivers output only once, at the end (TECH-DESIGN
section 7.4.5). File transfer uses SFTP and applies the I11 download
sanitisation.

POSIX timeouts are enforced by a small shell wrapper: the command runs in its
own process group when ``setsid`` exists and the wrapper kills the whole tree
(group first, then a parent/child walk) before returning exit code 124. Windows
timeouts use a PowerShell ``Start-Process`` wrapper and ``taskkill /T /F``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import math
import os
import posixpath
import re
import shlex
import stat
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import asyncssh

from evalspleef.spi.caps import OSFamily
from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxSpec,
)

if TYPE_CHECKING:
    from .provider import LibvirtProvider

_WORKSPACE = "/workspace"
_WINDOWS_WORKSPACE = "C:\\workspace"
_DEFAULT_MAX_BYTES = 1 << 30
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TIMEOUT_WRAPPER = """\
limit=$1
shift
marker=$(mktemp -d 2>/dev/null) || marker=/tmp/spleef-exec-$$
kill_tree() {
    for child in $(ps -o pid= -o ppid= -A 2>/dev/null | awk -v p="$1" '$2 == p { print $1 }'); do
        kill_tree "$child"
    done
    kill "$1" 2>/dev/null
}
"$@" <&0 &
pid=$!
(
    sleep "$limit"
    : > "$marker/timed-out"
    kill_tree "$pid"
    kill -TERM -"$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    sleep 1
    kill_tree "$pid"
    kill -KILL -"$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
) </dev/null >/dev/null 2>&1 &
watcher=$!
wait "$pid"
status=$?
kill "$watcher" 2>/dev/null
wait "$watcher" 2>/dev/null
if [ -f "$marker/timed-out" ]; then
    rm -rf "$marker"
    exit 124
fi
rm -rf "$marker"
exit "$status"
"""


class _ConnectError(Exception):
    """The SSH endpoint is not answering; the caller decides whether to fall back."""


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


def _validate_env(env: Mapping[str, str]) -> list[tuple[str, str]]:
    pairs = []
    for key, value in env.items():
        if not _ENV_NAME_RE.match(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        pairs.append((key, str(value)))
    return pairs


def posix_command(
    argv: list[str],
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
    timeout_s: float | None,
) -> str:
    """Build the shell string for one POSIX guest exec."""
    command = list(argv)
    if timeout_s is not None and timeout_s > 0:
        command = [
            "/bin/sh",
            "-c",
            _TIMEOUT_WRAPPER,
            "spleef-exec",
            f"{timeout_s:.3f}",
            *command,
        ]
    prefix: list[str] = []
    if cwd:
        prefix.append(f"cd {shlex.quote(cwd)}")
    joined = shlex.join(command)
    if env:
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in _validate_env(env))
        joined = f"env {assignments} {joined}"
    return " && ".join([*prefix, joined]) if prefix else joined


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_transport(script: str) -> str:
    """Wrap a PowerShell script so it travels safely through the Windows SSH shell."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        "powershell -NoProfile -NonInteractive -Command "
        '"[ScriptBlock]::Create([Text.Encoding]::UTF8.GetString('
        f"[Convert]::FromBase64String('{encoded}'))).Invoke()\""
    )


def windows_script(
    argv: list[str] | None,
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
    timeout_s: float | None,
    command_text: str | None = None,
) -> str:
    """Build the PowerShell script for one Windows guest exec (no transport wrapper).

    ``argv`` is invoked with the PowerShell call operator; a ``command_text``
    string is evaluated as-is with the usual ``-Command`` semantics.
    """
    if (argv is None) == (command_text is None):
        raise ValueError("pass exactly one of argv or command_text")
    lines: list[str] = ["$ErrorActionPreference = 'Continue'"]
    if cwd:
        lines.append(f"Set-Location -LiteralPath {_ps_quote(cwd)}")
    if env:
        for key, value in _validate_env(env):
            lines.append(f"$env:{key} = {_ps_quote(value)}")
    if command_text is not None:
        lines.append(command_text)
    else:
        assert argv is not None
        lines.append("& " + " ".join(_ps_quote(part) for part in argv))
    lines.append("exit $LASTEXITCODE")
    script = "\n".join(lines)
    if timeout_s is None or timeout_s <= 0:
        return script
    inner = base64.b64encode(script.encode("utf-8")).decode("ascii")
    milliseconds = math.ceil(timeout_s * 1000)
    return "\n".join(
        [
            "$ErrorActionPreference = 'Continue'",
            f"$inner = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{inner}'))",
            "$child = Start-Process -FilePath 'powershell' "
            "-ArgumentList @('-NoProfile','-NonInteractive','-Command',$inner) "
            "-PassThru -NoNewWindow",
            f"if (-not $child.WaitForExit({milliseconds})) {{",
            "    & taskkill.exe /PID $child.Id /T /F | Out-Null",
            "    exit 124",
            "}",
            "exit $child.ExitCode",
        ]
    )


def windows_command(
    argv: list[str],
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
    timeout_s: float | None,
) -> str:
    """The full remote command string for one Windows guest exec."""
    return _powershell_transport(windows_script(argv, cwd=cwd, env=env, timeout_s=timeout_s))


@dataclass(frozen=True)
class _UploadEntry:
    kind: str
    local: Path
    remote: str
    mode: int


def _plan_upload(source: Path, remote: str) -> list[_UploadEntry]:
    """Enumerate a local tree for upload; rejects symlinks and special files."""
    entries: list[_UploadEntry] = []
    if not source.exists() and not source.is_symlink():
        raise TransferError(f"local path does not exist: {source}")

    def walk(path: Path, destination: str) -> None:
        if path.is_symlink():
            raise TransferError(
                f"uploading symlinks is not supported: {path} (point at {path.readlink()})"
            )
        if path.is_dir():
            entries.append(_UploadEntry("dir", path, destination, 0o755))
            for child in sorted(path.iterdir()):
                walk(child, posixpath.join(destination, child.name))
            return
        if path.is_file():
            entries.append(
                _UploadEntry("file", path, destination, stat.S_IMODE(path.stat().st_mode))
            )
            return
        raise TransferError(
            f"cannot upload special file {path} (fifo/socket/device are not allowed)"
        )

    walk(source, remote)
    return entries


def _replace_with_symlink(path: Path, link: str) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(link)


class LibvirtSandbox(Sandbox):
    """One live domain; safe for concurrent ``exec`` calls."""

    def __init__(
        self,
        provider: LibvirtProvider,
        sandbox_id: str,
        spec: SandboxSpec,
        *,
        overlay_path: Path | None,
        ssh_host: str,
        ssh_port: int,
    ) -> None:
        self._provider = provider
        self.id = sandbox_id
        self.spec = spec
        self.overlay_path = overlay_path
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self._connection: asyncssh.SSHClientConnection | None = None
        self._connection_lock = asyncio.Lock()
        self._destroyed = False
        self._lost = False

    # -- liveness ---------------------------------------------------------------

    @property
    def lost(self) -> bool:
        return self._lost

    def mark_lost(self) -> None:
        self._lost = True

    def mark_destroyed(self) -> None:
        self._destroyed = True

    def _check_live(self) -> None:
        if self._destroyed:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "destroyed"})
        if self._lost:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "vanished"})

    # -- connection -------------------------------------------------------------

    async def _connection_or_raise(self) -> asyncssh.SSHClientConnection:
        async with self._connection_lock:
            connection = self._connection
            if connection is not None and not connection.is_closed():
                return connection
            provider = self._provider
            options: dict[str, Any] = {
                "username": provider.ssh_user,
                "known_hosts": provider.ssh_known_hosts,
                "encoding": None,
                "connect_timeout": provider.ssh_connect_timeout_s,
            }
            if provider.ssh_key_file is not None:
                options["client_keys"] = [provider.ssh_key_file]
            try:
                connection = await asyncssh.connect(self.ssh_host, self.ssh_port, **options)
            except (TimeoutError, OSError, asyncssh.Error) as exc:
                raise _ConnectError(f"ssh to {self.ssh_host}:{self.ssh_port}: {exc}") from exc
            self._connection = connection
            return connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
                await connection.wait_closed()

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
        if user is not None and user != self._provider.ssh_user:
            raise CapabilityNotSupported(
                f"libvirt exec cannot switch to user {user!r}; the SSH account is "
                f"{self._provider.ssh_user!r}"
            )
        if self.spec.os is OSFamily.WINDOWS:
            return await self._exec_windows(
                cmd,
                cwd=cwd or _WINDOWS_WORKSPACE,
                env=env,
                timeout_s=timeout_s,
                stdin=stdin,
                on_output=on_output,
                max_output_bytes=max_output_bytes,
            )
        return await self._exec_posix(
            cmd,
            cwd=cwd or _WORKSPACE,
            env=env,
            timeout_s=timeout_s,
            stdin=stdin,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
        )

    async def _exec_posix(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout_s: float | None,
        stdin: bytes | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> ExecResult:
        argv = ["/bin/sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        command = posix_command(argv, cwd=cwd, env=env, timeout_s=timeout_s)
        try:
            connection = await self._connection_or_raise()
        except _ConnectError as exc:
            if self._provider.guest_agent_fallback:
                return await self._exec_guest_agent(
                    argv,
                    cwd=None if cwd == _WORKSPACE else cwd,
                    env=env,
                    timeout_s=timeout_s,
                    stdin=stdin,
                    on_output=on_output,
                    cause=str(exc),
                )
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": "ssh unreachable", "cause": str(exc)}
            ) from exc
        return await self._run_process(
            connection,
            command.encode(),
            timeout_s=timeout_s,
            stdin=stdin,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
        )

    async def _exec_windows(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout_s: float | None,
        stdin: bytes | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> ExecResult:
        if isinstance(cmd, str):
            script = windows_script(None, cwd=cwd, env=env, timeout_s=None, command_text=cmd)
            if timeout_s is not None and timeout_s > 0:
                script = _windows_timeout(script, timeout_s)
            command = _powershell_transport(script)
        else:
            command = windows_command(list(cmd), cwd=cwd, env=env, timeout_s=timeout_s)
        try:
            connection = await self._connection_or_raise()
        except _ConnectError as exc:
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": "ssh unreachable", "cause": str(exc)}
            ) from exc
        return await self._run_process(
            connection,
            command.encode(),
            timeout_s=timeout_s,
            stdin=stdin,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
        )

    async def _run_process(
        self,
        connection: asyncssh.SSHClientConnection,
        command: bytes,
        *,
        timeout_s: float | None,
        stdin: bytes | None,
        on_output: OutputCallback | None,
        max_output_bytes: int,
    ) -> ExecResult:
        stdout = _CappedBuffer(max_output_bytes)
        stderr = _CappedBuffer(max_output_bytes)
        started = time.monotonic()
        timed_out = False
        try:
            process = await connection.create_process(command)
        except (OSError, asyncssh.Error) as exc:
            if self.spec.os is OSFamily.WINDOWS or not self._provider.guest_agent_fallback:
                raise SandboxLost(
                    detail={"sandbox_id": self.id, "reason": "ssh exec failed", "cause": str(exc)}
                ) from exc
            return await self._exec_guest_agent(
                ["/bin/sh", "-lc", command.decode()],
                cwd=None,
                env=None,
                timeout_s=timeout_s,
                stdin=stdin,
                on_output=on_output,
                cause=str(exc),
            )

        async def pump(reader: Any, buffer: _CappedBuffer, stream: str) -> None:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                buffer.append(chunk)
                if on_output is not None:
                    on_output("stdout" if stream == "stdout" else "stderr", chunk)

        async def feed_stdin() -> None:
            if stdin is None:
                return
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.write_eof()

        watchdog = None if timeout_s is None else timeout_s + 20.0
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    pump(process.stdout, stdout, "stdout"),
                    pump(process.stderr, stderr, "stderr"),
                    feed_stdin(),
                ),
                timeout=watchdog,
            )
            completed = await process.wait()
            exit_code = int(completed.returncode or 0)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(Exception):
                process.kill()
                await process.wait()
            exit_code = TIMEOUT_EXIT_CODE
        duration_s = time.monotonic() - started
        if exit_code == TIMEOUT_EXIT_CODE:
            timed_out = True
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=stdout.truncated or stderr.truncated,
        )

    async def _exec_guest_agent(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout_s: float | None,
        stdin: bytes | None,
        on_output: OutputCallback | None,
        cause: str,
    ) -> ExecResult:
        """qemu-guest-agent ``guest-exec`` fallback: no streaming, no cwd/env/user."""
        if cwd is not None or env:
            raise CapabilityNotSupported(
                "the qemu-guest-agent fallback cannot honour cwd or env; "
                f"SSH is unavailable ({cause})"
            )
        if stdin is not None:
            raise CapabilityNotSupported(
                f"the qemu-guest-agent fallback cannot deliver stdin; SSH is unavailable ({cause})"
            )
        command = (
            argv[-1] if argv[:2] in (["/bin/sh", "-lc"], ["/bin/sh", "-c"]) else shlex.join(argv)
        )
        started = time.monotonic()
        if self.spec.os is OSFamily.WINDOWS:
            payload = {
                "path": "powershell.exe",
                "arg": ["-NoProfile", "-NonInteractive", "-Command", command],
                "capture-output": True,
            }
        else:
            payload = {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            }
        started_reply = await self._provider.guest_agent_command(
            self.id, {"execute": "guest-exec", "arguments": payload}
        )
        pid = int(started_reply.get("pid", -1))
        deadline = None if timeout_s is None else started + timeout_s
        while True:
            status = await self._provider.guest_agent_command(
                self.id, {"execute": "guest-exec-status", "arguments": {"pid": pid}}
            )
            if status.get("exited"):
                break
            if deadline is not None and time.monotonic() >= deadline:
                return ExecResult(
                    exit_code=TIMEOUT_EXIT_CODE,
                    stdout=b"",
                    stderr=b"",
                    timed_out=True,
                    duration_s=time.monotonic() - started,
                )
            await asyncio.sleep(0.05)
        out = _decode_agent_data(status.get("out-data"))
        err = _decode_agent_data(status.get("err-data"))
        if on_output is not None:
            if out:
                on_output("stdout", out)
            if err:
                on_output("stderr", err)
        return ExecResult(
            exit_code=int(status.get("exitcode", 0)),
            stdout=out,
            stderr=err,
            timed_out=False,
            duration_s=time.monotonic() - started,
        )

    # -- file transfer ----------------------------------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._check_live()
        if not remote.startswith("/"):
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        source = Path(local)
        try:
            entries = await asyncio.to_thread(_plan_upload, source, remote)
        except TransferError:
            raise
        except OSError as exc:
            raise TransferError(f"cannot read {source}: {exc}") from exc
        try:
            sftp = await self._sftp()
        except (OSError, asyncssh.Error) as exc:
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": "sftp unreachable", "cause": str(exc)}
            ) from exc
        try:
            for entry in entries:
                if entry.kind == "dir":
                    await sftp.makedirs(entry.remote, exist_ok=True)
                    with contextlib.suppress(asyncssh.SFTPError):
                        await sftp.chmod(entry.remote, entry.mode)
                    continue
                parent = posixpath.dirname(entry.remote) or "/"
                await sftp.makedirs(parent, exist_ok=True)
                await sftp.put(str(entry.local), entry.remote)
                with contextlib.suppress(asyncssh.SFTPError):
                    await sftp.chmod(entry.remote, entry.mode)
        except TransferError:
            raise
        except (OSError, asyncssh.Error) as exc:
            raise TransferError(f"upload to {remote!r} failed: {exc}") from exc

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        if not remote.startswith("/"):
            raise TransferError(f"remote path must be absolute, got {remote!r}")
        try:
            sftp = await self._sftp()
        except (OSError, asyncssh.Error) as exc:
            raise SandboxLost(
                detail={"sandbox_id": self.id, "reason": "sftp unreachable", "cause": str(exc)}
            ) from exc
        cap = _DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
        try:
            await self._download_path(sftp, remote, Path(local), total=0, cap=cap, root=remote)
        except TransferError:
            raise
        except asyncssh.SFTPNoSuchFile as exc:
            raise TransferError(f"remote path does not exist: {remote!r}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise TransferError(f"download of {remote!r} failed: {exc}") from exc

    async def _download_path(
        self,
        sftp: asyncssh.SFTPClient,
        remote: str,
        local: Path,
        *,
        total: int,
        cap: int,
        root: str,
    ) -> int:
        attrs = await sftp.lstat(remote)
        mode = attrs.permissions if attrs.permissions is not None else 0o644
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK:
            link = await sftp.readlink(remote)
            if os.path.isabs(link) or ".." in PurePosixPath(link).parts:
                raise TransferError(
                    f"refusing symlink escaping the sandbox export: {remote} -> {link}"
                )
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(remote), link))
            if not (resolved == root or resolved.startswith(root.rstrip("/") + "/")):
                raise TransferError(
                    f"refusing symlink escaping the sandbox export: {remote} -> {link}"
                )
            await asyncio.to_thread(local.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_replace_with_symlink, local, link)
            return total
        if kind == stat.S_IFDIR:
            await asyncio.to_thread(local.mkdir, parents=True, exist_ok=True)
            entries = [entry async for entry in sftp.scandir(remote)]
            for entry in entries:
                name = (
                    entry.filename.decode("utf-8")
                    if isinstance(entry.filename, bytes)
                    else entry.filename
                )
                if name in (".", ".."):
                    continue
                total = await self._download_path(
                    sftp,
                    posixpath.join(remote, name),
                    local / name,
                    total=total,
                    cap=cap,
                    root=root,
                )
            return total
        if kind != stat.S_IFREG:
            raise TransferError(f"refusing special file from sandbox: {remote}")
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise TransferError(f"refusing setuid/setgid file from sandbox: {remote}")
        size = int(attrs.size or 0)
        if total + size > cap:
            raise TransferError(
                f"download exceeds max_bytes={cap} (member {remote} is {size} bytes)"
            )
        await asyncio.to_thread(local.parent.mkdir, parents=True, exist_ok=True)
        await sftp.get(remote, str(local))
        await asyncio.to_thread(local.chmod, stat.S_IMODE(mode))
        return total + size

    async def _sftp(self) -> asyncssh.SFTPClient:
        connection = await self._connection_or_raise()
        return await connection.start_sftp_client()

    # -- optional capabilities --------------------------------------------------

    async def gui_endpoint(self) -> str:
        self._check_live()
        support = self._provider.capabilities().gui
        if support == "none":
            raise CapabilityNotSupported("gui")
        if support == "rdp" and self.spec.os is not OSFamily.WINDOWS:
            raise CapabilityNotSupported(f"gui {support!r} for os {self.spec.os.value!r}")
        port = self._provider.gui_port(support)
        scheme = "rdp" if support == "rdp" else "vnc"
        return f"{scheme}://{self.ssh_host}:{port}"

    async def describe(self) -> dict[str, Any]:
        return {
            "domain": self.id,
            "host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "overlay": str(self.overlay_path) if self.overlay_path else None,
            "template_digest": self.spec.template.digest,
        }

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._provider.release_sandbox(self)


def _windows_timeout(script: str, timeout_s: float) -> str:
    """Wrap a PowerShell script with a process-tree timeout and exit code 124."""
    inner = base64.b64encode(script.encode("utf-8")).decode("ascii")
    milliseconds = math.ceil(timeout_s * 1000)
    return "\n".join(
        [
            "$ErrorActionPreference = 'Continue'",
            f"$inner = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{inner}'))",
            "$child = Start-Process -FilePath 'powershell' "
            "-ArgumentList @('-NoProfile','-NonInteractive','-Command',$inner) "
            "-PassThru -NoNewWindow",
            f"if (-not $child.WaitForExit({milliseconds})) {{",
            "    & taskkill.exe /PID $child.Id /T /F | Out-Null",
            "    exit 124",
            "}",
            "exit $child.ExitCode",
        ]
    )


def _decode_agent_data(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError):
        return b""
