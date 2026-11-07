"""A local asyncssh server emulating the guest's OpenSSH endpoint for tests.

The fake guest executes command lines through ``/bin/sh`` (with the VM's
workspace as cwd) and additionally understands the small PowerShell-like
subset the adapter and the conformance cases emit:

- ``powershell -NoProfile -NonInteractive -EncodedCommand <utf-16-le b64>``
  with ``Write-Output [-NoNewline]``, ``[Console]::Out/Error.Write``,
  ``Start-Sleep -Seconds|-Milliseconds``, ``$env:NAME = 'value'``,
  ``Set-Location -LiteralPath``, string-repeat assignment (``$s = 'x' * N``),
  ``$pid | Out-File`` pid files, ``Expand-Archive``/``Compress-Archive`` and
  native command fallback (``sh -c ...`` and friends);
- ``cmd /c exit|mkdir|mklink|type|rmdir|taskkill``;
- a bare ``taskkill /T /F /PID <n>`` against the fake process table.

SFTP is served with ``asyncssh.SFTPServer`` chrooted at the fake VM directory,
so uploads/downloads touch real files under the VM's directory. Everything is
executed by the local ``/bin/sh``: this proves the provider's SSH/SFTP
plumbing end to end but is **not** a Windows guest and not verified on a real
host.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import signal
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh

TIMEOUT_EXIT_CODE = 124
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


class _ProcessTable:
    """Fake guest process table: fake pid -> running task or host subprocess."""

    def __init__(self) -> None:
        self._next_pid = 4000
        self._entries: dict[int, Any] = {}
        self._idle = asyncio.Event()
        self._idle.set()

    def register(self, handle: Any) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self._entries[pid] = handle
        self._idle.clear()
        return pid

    def unregister(self, pid: int) -> None:
        self._entries.pop(pid, None)
        if not self._entries:
            self._idle.set()

    def pids(self) -> list[int]:
        return sorted(self._entries)

    async def wait_idle(self, within_s: float) -> None:
        """Wait until every registered fake process has finished."""
        await asyncio.wait_for(self._idle.wait(), within_s)

    def kill(self, pid: int) -> bool:
        handle = self._entries.get(pid)
        if handle is None:
            return False
        if isinstance(handle, asyncio.Task):
            handle.cancel()
            return True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(handle.pid, signal.SIGKILL)
        return True


class FakeGuestServer:
    """One fake guest: an asyncssh server rooted at the fake VM directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = 0
        self.commands: list[str] = []
        self.processes = _ProcessTable()
        self._server: asyncssh.SSHAcceptor | None = None
        self._host_key: asyncssh.SSHKey | None = None

    async def start(self) -> None:
        self._host_key = asyncssh.generate_private_key("ssh-ed25519")
        server = await asyncssh.create_server(
            lambda: _FakeSSHServer(self),
            "127.0.0.1",
            0,
            server_host_keys=[self._host_key],
            process_factory=lambda process: _handle_process(self, process),
            encoding=None,
            sftp_factory=lambda chan: _FakeSFTPServer(chan, chroot=str(self.root).encode()),
        )
        self._server = server
        self.port = int(server.get_addresses()[0][1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for pid in self.processes.pids():
            self.processes.kill(pid)


class _ExecContext:
    """The interpreter state for one exec channel."""

    def __init__(self, guest: FakeGuestServer, process: Any) -> None:
        self.guest = guest
        self.process = process
        self.root = guest.root
        self.workspace = guest.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cwd = self.workspace
        self.env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(guest.root / "home" / "agent"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "COMPUTERNAME": "EVALGUEST01",
            "COPYFILE_DISABLE": "1",  # keep a macOS host tar from adding ._* AppleDouble members
        }
        self.pid = 0
        self.variables: dict[str, str] = {}

    # -- output ---------------------------------------------------------------

    def out(self, data: str | bytes) -> None:
        self.process.stdout.write(data.encode() if isinstance(data, str) else data)

    def err(self, data: str | bytes) -> None:
        self.process.stderr.write(data.encode() if isinstance(data, str) else data)

    # -- guest path mapping ------------------------------------------------------

    def map_path(self, raw: str) -> Path:
        """Translate a guest path (``/workspace/...``, ``C:/...``) to the host."""
        return map_guest_path(self.root, raw, self.cwd)


def map_guest_path(root: Path, raw: str, cwd: Path | None = None) -> Path:
    """Map a guest path onto the fake VM directory ``root`` (``workspace/`` is the workspace)."""
    cleaned = raw.strip().strip("'").replace("\\", "/")
    drive = _WINDOWS_ABS_RE.match(cleaned)
    if drive and cleaned[2:].startswith("/workspace"):
        cleaned = cleaned[2:]  # C:\workspace\x is the same place as /workspace/x
    pure = PurePosixPath(cleaned)
    if cleaned == "/workspace" or cleaned.startswith("/workspace/"):
        return (root / "workspace").joinpath(*pure.parts[2:])
    if pure.is_absolute():
        return root.joinpath(*pure.parts[1:])
    if drive:
        return root / cleaned[0].upper() / cleaned[3:]
    return (cwd if cwd is not None else root / "workspace") / cleaned


class _FakeSFTPServer(asyncssh.SFTPServer):
    """SFTP chrooted at the VM directory that, like a real sftp-server, returns raw link targets."""

    async def readlink(self, path: bytes) -> bytes:
        return os.readlink(self.map_path(path))


class _FakeSSHServer(asyncssh.SSHServer):
    def __init__(self, guest: FakeGuestServer) -> None:
        self._guest = guest

    def begin_auth(self, username: str) -> bool:
        return False  # the fake accepts any client without authentication


async def _handle_process(guest: FakeGuestServer, process: Any) -> None:
    """Run one exec request as a task registered in the fake process table.

    The task is cancelled when a ``taskkill`` names its fake pid or when the
    client closes the channel; a cancelled command reports exit code 124.
    """
    command = str(process.command or "")
    ctx = _ExecContext(guest, process)
    guest.commands.append(command)
    work = asyncio.ensure_future(dispatch_command(command, ctx))
    ctx.pid = guest.processes.register(work)
    closed = asyncio.ensure_future(process.wait_closed())
    exit_code = 1
    try:
        await asyncio.wait({work, closed}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            work.cancel()
        try:
            exit_code = await work
        except asyncio.CancelledError:
            exit_code = TIMEOUT_EXIT_CODE
        except Exception as exc:  # a fake bug must be visible, not silent
            ctx.err(f"fake guest error: {exc!r}\n")
            exit_code = 1
    finally:
        closed.cancel()
        guest.processes.unregister(ctx.pid)
    with contextlib.suppress(Exception):
        process.exit(exit_code)


# -- command dispatch ------------------------------------------------------------


async def dispatch_command(command: str, ctx: _ExecContext) -> int:
    if command.startswith("powershell"):
        script = _decode_powershell_invocation(command)
        if script is None:
            ctx.err("fake guest: unrecognized powershell invocation\n")
            return 1
        return await interpret_powershell(script, ctx)
    if command.startswith("cmd /c"):
        rest = command[len("cmd /c") :].strip()
        if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"' and rest.count('"') == 2:
            rest = rest[1:-1]  # cmd /c strips one pair of enclosing quotes
        return await interpret_cmd(rest, ctx)
    if command.startswith("taskkill"):
        return await interpret_cmd(command, ctx)
    return await _run_via_sh(command, ctx)


def _decode_powershell_invocation(command: str) -> str | None:
    encoded = re.search(r"\s-EncodedCommand\s+(\S+)\s*$", command)
    if encoded:
        try:
            return base64.b64decode(encoded.group(1)).decode("utf-16-le")
        except Exception:
            return None
    plain = re.search(r"\s-Command\s+(.*)$", command, re.DOTALL)
    if plain:
        return plain.group(1).strip()
    return None


async def _run_via_sh(command: str, ctx: _ExecContext) -> int:
    return await _run_argv(["/bin/sh", "-c", command], ctx)


async def _run_argv(argv: list[str], ctx: _ExecContext) -> int:
    """Run ``argv`` as a host subprocess in its own session, streaming I/O to the channel."""
    ctx.cwd.mkdir(parents=True, exist_ok=True)
    # Shell commands name the guest workspace as /workspace; the fake keeps it on the host.
    argv = [token.replace("/workspace", str(ctx.workspace)) for token in argv]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ctx.cwd),
            env={**os.environ, **ctx.env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        ctx.err(f"The term '{argv[0]}' is not recognized as the name of a cmdlet or program\n")
        return 1
    handle = ctx.guest.processes.register(process)
    feeder = asyncio.ensure_future(_feed_stdin(ctx, process))
    try:

        async def pump(stream: Any, sink: Any) -> None:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                sink(chunk)

        await asyncio.gather(
            pump(process.stdout, ctx.out),
            pump(process.stderr, ctx.err),
            process.wait(),
        )
        return int(process.returncode or 0)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        raise
    finally:
        feeder.cancel()
        ctx.guest.processes.unregister(handle)


async def _feed_stdin(ctx: _ExecContext, process: asyncio.subprocess.Process) -> None:
    """Forward the channel's stdin to the command until the client sends EOF."""
    data = await ctx.process.stdin.read()
    if process.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()


# -- PowerShell-like subset --------------------------------------------------------


def _split_statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_quote = False
    index = 0
    while index < len(script):
        char = script[index]
        if char == "'":
            current.append(char)
            if in_quote and index + 1 < len(script) and script[index + 1] == "'":
                current.append("'")
                index += 1
            else:
                in_quote = not in_quote
        elif char == ";" and not in_quote:
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current))
    return statements


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _flag_value(statement: str, flag: str) -> str | None:
    pattern = re.compile(rf"-{re.escape(flag)}\s+'((?:[^']|'')*)'")
    match = pattern.search(statement)
    if match:
        return match.group(1).replace("''", "'")
    bare = re.compile(rf"-{re.escape(flag)}\s+(\S+)")
    match = bare.search(statement)
    if match:
        return match.group(1)
    return None


def _eval_expr(expr: str, ctx: _ExecContext) -> str:
    expr = expr.strip().rstrip(";").strip()
    repeat = re.match(r"^'((?:[^']|'')*)'\s*\*\s*(\d+)$", expr)
    if repeat:
        return repeat.group(1).replace("''", "'") * int(repeat.group(2))
    if expr.startswith("$env:"):
        name = expr[len("$env:") :]
        return ctx.env.get(name, os.environ.get(name, ""))
    if expr.startswith("$"):
        return ctx.variables.get(expr[1:], "")
    return _unquote(expr)


async def interpret_powershell(script: str, ctx: _ExecContext) -> int:
    for statement in _split_statements(script):
        statement = statement.strip()
        if not statement or statement.startswith("#"):
            continue
        if re.match(r"^\$pid\s*\|\s*Out-File\b", statement):
            target = _flag_value(statement, "FilePath")
            if target is None:
                ctx.err("Out-File needs -FilePath\n")
                return 1
            path = ctx.map_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{ctx.pid}\n", encoding="ascii")
            continue
        env_assign = re.match(r"^\$env:(\w+)\s*=\s*(.+)$", statement, re.DOTALL)
        if env_assign:
            ctx.env[env_assign.group(1)] = _eval_expr(env_assign.group(2), ctx)
            continue
        var_assign = re.match(r"^\$(\w+)\s*=\s*(.+)$", statement, re.DOTALL)
        if var_assign:
            ctx.variables[var_assign.group(1)] = _eval_expr(var_assign.group(2), ctx)
            continue
        if statement.startswith("Set-Location"):
            target = _flag_value(statement, "LiteralPath") or _flag_value(statement, "Path")
            if target is None:
                ctx.err("Set-Location needs -LiteralPath\n")
                return 1
            ctx.cwd = ctx.map_path(target)
            ctx.cwd.mkdir(parents=True, exist_ok=True)
            continue
        if statement.startswith("Write-Output"):
            no_newline = "-NoNewline" in statement
            arg = statement.replace("-NoNewline", "", 1).replace("Write-Output", "", 1).strip()
            ctx.out(_eval_expr(arg, ctx) + ("" if no_newline else "\n"))
            continue
        console = re.match(r"^\[Console\]::(Out|Error)\.Write\((.*)\)$", statement, re.DOTALL)
        if console:
            text = _eval_expr(console.group(2), ctx)
            if console.group(1) == "Error":
                ctx.err(text)
            else:
                ctx.out(text)
            continue
        if statement.startswith("Start-Sleep"):
            seconds = _flag_value(statement, "Seconds")
            if seconds is None:
                milliseconds = _flag_value(statement, "Milliseconds")
                if milliseconds is None:
                    ctx.err("Start-Sleep needs -Seconds or -Milliseconds\n")
                    return 1
                await asyncio.sleep(float(milliseconds) / 1000.0)
            else:
                await asyncio.sleep(float(seconds))
            continue
        if statement.startswith("Expand-Archive"):
            source = _flag_value(statement, "LiteralPath") or _flag_value(statement, "Path")
            dest = _flag_value(statement, "DestinationPath")
            if source is None or dest is None:
                ctx.err("Expand-Archive needs -LiteralPath and -DestinationPath\n")
                return 1
            try:
                _expand_archive(ctx.map_path(source), ctx.map_path(dest))
            except Exception as exc:
                ctx.err(f"Expand-Archive failed: {exc}\n")
                return 1
            continue
        if statement.startswith("Remove-Item"):
            target = _flag_value(statement, "LiteralPath") or _flag_value(statement, "Path")
            if target is None:
                ctx.err("Remove-Item needs -LiteralPath\n")
                return 1
            await asyncio.to_thread(_remove_path, ctx.map_path(target))
            continue
        if statement.startswith("Compress-Archive"):
            source = _flag_value(statement, "Path") or _flag_value(statement, "LiteralPath")
            dest = _flag_value(statement, "DestinationPath")
            if source is None or dest is None:
                ctx.err("Compress-Archive needs -Path and -DestinationPath\n")
                return 1
            try:
                _compress_archive(ctx.map_path(source), ctx.map_path(dest))
            except Exception as exc:
                ctx.err(f"Compress-Archive failed: {exc}\n")
                return 1
            continue
        # Native command fallback (& 'sh' '-c' '...'): run the tokenised argv directly.
        argv = _ps_tokens(statement)
        if argv:
            code = await _run_argv(argv, ctx)
            if code != 0:
                return code
    return 0


def _ps_tokens(statement: str) -> list[str]:
    """Split one PowerShell native-command statement (single quotes, ``''`` escapes)."""
    text = statement.strip()
    if text.startswith("& "):
        text = text[2:].lstrip()
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    has_token = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_quote:
            if char == "'" and index + 1 < len(text) and text[index + 1] == "'":
                current.append("'")
                index += 1
            elif char == "'":
                in_quote = False
            else:
                current.append(char)
        elif char == "'":
            in_quote = True
            has_token = True
        elif char.isspace():
            if has_token:
                tokens.append("".join(current))
                current = []
                has_token = False
        else:
            current.append(char)
            has_token = True
        index += 1
    if has_token:
        tokens.append("".join(current))
    return tokens


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _expand_archive(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe member {member.filename!r}")
            target = dest.joinpath(*relative.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as handle:
                target.write_bytes(handle.read())
            mode = member.external_attr >> 16
            if mode:
                target.chmod(stat.S_IMODE(mode))


def _compress_archive(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                relative = child.relative_to(source.parent)
                name = str(relative).replace(os.sep, "/")
                info = zipfile.ZipInfo(name + "/" if child.is_dir() else name)
                info.external_attr = (child.stat().st_mode & 0xFFFF) << 16
                if child.is_dir():
                    archive.writestr(info, b"")
                else:
                    archive.writestr(info, child.read_bytes())
        else:
            info = zipfile.ZipInfo(source.name)
            info.external_attr = (source.stat().st_mode & 0xFFFF) << 16
            archive.writestr(info, source.read_bytes())


# -- cmd.exe-like subset -------------------------------------------------------------


def _split_cmd_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    for char in text:
        if char == '"':
            in_quote = not in_quote
            continue
        if char in " \t" and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


async def interpret_cmd(text: str, ctx: _ExecContext) -> int:
    tokens = _split_cmd_tokens(text)
    if not tokens:
        return 1
    command, args = tokens[0], tokens[1:]
    if command == "exit":
        return int(args[0]) if args else 0
    if command == "mkdir":
        for name in args:
            if name.startswith("-"):
                continue
            ctx.map_path(name).mkdir(parents=True, exist_ok=True)
        return 0
    if command == "rmdir":
        for name in args:
            if name.startswith("-"):
                continue
            shutil.rmtree(ctx.map_path(name), ignore_errors=True)
        return 0
    if command == "mklink":
        if len(args) < 2:
            ctx.err("usage: mklink <link> <target>\n")
            return 1
        link = ctx.map_path(args[0])
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(args[1])
        return 0
    if command == "type":
        if not args:
            ctx.err("usage: type <file>\n")
            return 1
        path = ctx.map_path(args[0])
        if not path.is_file():
            ctx.err(f"The system cannot find the file: {args[0]}\n")
            return 1
        ctx.out(path.read_text(encoding="utf-8", errors="replace"))
        return 0
    if command == "taskkill":
        pid = None
        for index, token in enumerate(args):
            if token.upper() == "/PID" and index + 1 < len(args):
                pid = int(args[index + 1])
        if pid is None:
            ctx.err("taskkill needs /PID\n")
            return 1
        return 0 if ctx.guest.processes.kill(pid) else 1
    ctx.err(f"'{command}' is not recognized\n")
    return 1
