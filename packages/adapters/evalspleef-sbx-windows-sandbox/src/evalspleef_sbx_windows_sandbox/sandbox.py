"""Sandbox instance backed by one Windows Sandbox (``wsb``) instance.

The ``wsb`` CLI returns only an exit code from ``exec``, so every command is
wrapped so that stdout and stderr are redirected into the mapped workspace
folder (``.spleef/<run>.stdout`` and ``.stderr``) and read back after the
command finished. ``on_output`` therefore fires once, at the end, and the
provider declares ``streaming=False``.

File transfer is a copy against the host side of the mapped folder: Windows
Sandbox mounts exactly one host directory and nothing else, so ``/workspace``
in the sandbox is the per-instance directory the provider created.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxSpec,
)

OUTPUT_DIR = ".spleef"
"""Directory under the mapped workspace that receives per-exec output files."""


def _read_capped(path: Path, cap: int) -> tuple[bytes, bool]:
    """Read at most ``cap`` bytes of ``path``; also report whether it was longer."""
    if not path.is_file():
        return b"", False
    size = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(cap)
    return data, size > cap


def _inside(root: Path, candidate: Path) -> bool:
    root_resolved = root.resolve()
    try:
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    return os.path.commonpath([str(root_resolved), str(candidate_resolved)]) == str(root_resolved)


def _check_source_tree(root: Path) -> None:
    """Refuse uploads whose symlinks or special files would escape the source tree."""
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        for name in [*dirnames, *filenames]:
            path = base / name
            if path.is_symlink():
                if not _inside(root, path):
                    raise TransferError(f"upload contains a symlink escaping the tree: {path}")
            elif not (path.is_dir() or path.is_file()):
                raise TransferError(f"upload contains a special file: {path}")


def _copy_upload(local: Path, target: Path) -> None:
    """Copy a file or directory into the mapped workspace."""
    if local.is_dir():
        _check_source_tree(local)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local, target, dirs_exist_ok=True, copy_function=shutil.copy2)
    elif local.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)
    else:
        raise TransferError(f"upload source {local} is neither a file nor a directory")


def _tree_size(root: Path) -> int:
    total = 0
    for current, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(current) / name
            if path.is_symlink():
                raise TransferError(f"download contains a symlink: {path}")
            if not path.is_file():
                raise TransferError(f"download contains a special file: {path}")
            total += path.stat().st_size
    return total


class WindowsSandbox(Sandbox):
    """One live Windows Sandbox instance; safe for concurrent ``exec`` calls."""

    def __init__(
        self,
        provider: Any,
        instance_id: str,
        spec: SandboxSpec,
        workspace: Path,
    ) -> None:
        self._provider = provider
        self.id = instance_id
        self.spec = spec
        self._workspace = workspace
        self._destroyed = False

    # -- lifecycle ---------------------------------------------------------------

    def _check_live(self) -> None:
        if self._destroyed:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "destroyed"})
        entry = self._provider.entry(self.id)
        if entry is None:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "instance is gone"})
        expires_at = float(entry.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "lifetime expired"})

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._provider.release_sandbox(self.id)

    async def describe(self) -> dict[str, Any]:
        entry = self._provider.entry(self.id)
        if entry is None:
            return {}
        return {
            "host": "windows-sandbox",
            "guest_os": entry.get("guest_os", ""),
            "template_digest": entry.get("template", {}).get("digest", ""),
            "workspace": str(self._workspace),
            "expires_at": entry.get("expires_at", 0),
        }

    # -- exec --------------------------------------------------------------------

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
        if stdin is not None:
            raise CapabilityNotSupported("windows-sandbox declares stdin=False")
        if user is not None and user != self.spec.run_as_user:
            raise CapabilityNotSupported(
                f"windows-sandbox always runs as {self.spec.run_as_user!r}, cannot switch to "
                f"{user!r}"
            )
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")

        output_dir = self._workspace / OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        out_host = output_dir / f"{run_id}.stdout"
        err_host = output_dir / f"{run_id}.stderr"
        out_guest = self._provider.guest_path(
            f"/workspace/{OUTPUT_DIR}/{run_id}.stdout", self._workspace
        )
        err_guest = self._provider.guest_path(
            f"/workspace/{OUTPUT_DIR}/{run_id}.stderr", self._workspace
        )
        command = self._render(cmd, cwd, env or {}, out_guest, err_guest)

        effective_timeout = self._provider.exec_timeout_s if timeout_s is None else timeout_s
        started = time.monotonic()
        exit_code, _cli_out, _cli_err, timed_out = await self._provider.run_wsb(
            ["exec", "--id", self.id, "-c", command, "-r", "ExistingLogin"],
            timeout_s=max(0.01, effective_timeout),
            workspace=self._workspace,
        )
        duration_s = time.monotonic() - started

        stdout, out_truncated = _read_capped(out_host, max_output_bytes)
        stderr, err_truncated = _read_capped(err_host, max_output_bytes)
        out_host.unlink(missing_ok=True)
        err_host.unlink(missing_ok=True)

        if on_output is not None:
            if stdout:
                on_output("stdout", stdout)
            if stderr:
                on_output("stderr", stderr)

        return ExecResult(
            exit_code=TIMEOUT_EXIT_CODE if timed_out else exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=out_truncated or err_truncated,
        )

    def _render(
        self,
        cmd: str | list[str],
        cwd: str | None,
        env: Mapping[str, str],
        out_guest: str,
        err_guest: str,
    ) -> str:
        if isinstance(cmd, list):
            argv = [str(argument) for argument in cmd]
            command = (
                " ".join(shlex.quote(argument) for argument in argv)
                if self._provider.guest_os.value == "linux"
                else subprocess.list2cmdline(argv)
            )
        else:
            command = cmd
        workdir = self._provider.guest_path(cwd or "/workspace", self._workspace)
        if self._provider.guest_os.value == "linux":
            prefix = ""
            if env:
                assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
                prefix = f"export {assignments}; "
            # The whole command runs in a subshell so that per-exec output
            # redirection covers every statement of a compound command.
            return (
                f"{prefix}( cd {shlex.quote(workdir)} && {command} ) "
                f"> {shlex.quote(out_guest)} 2> {shlex.quote(err_guest)}"
            )
        prefix = "".join(f'set "{key}={value}" && ' for key, value in env.items())
        return f'{prefix}( cd /d "{workdir}" && {command} ) > "{out_guest}" 2> "{err_guest}"'

    # -- file transfer -----------------------------------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._check_live()
        target = self._provider.host_path(remote, self._workspace)
        try:
            _copy_upload(local, target)
        except TransferError:
            raise
        except OSError as exc:
            raise TransferError(f"upload of {local} to {remote!r} failed: {exc}") from exc

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        source = self._provider.host_path(remote, self._workspace)
        if not source.is_symlink() and not source.exists():
            raise TransferError(f"{remote!r} does not exist in the sandbox")
        if not _inside(self._workspace, source):
            raise TransferError(f"download of {remote!r} escapes the mapped workspace")
        try:
            if source.is_dir():
                if max_bytes is not None and _tree_size(source) > max_bytes:
                    raise TransferError(f"download of {remote!r} exceeds {max_bytes} bytes")
                shutil.copytree(source, local, dirs_exist_ok=True, copy_function=shutil.copy2)
            else:
                if max_bytes is not None and source.stat().st_size > max_bytes:
                    raise TransferError(f"download of {remote!r} exceeds {max_bytes} bytes")
                local.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, local)
        except TransferError:
            raise
        except OSError as exc:
            raise TransferError(f"download of {remote!r} to {local} failed: {exc}") from exc

    # -- helpers used by tests ----------------------------------------------------

    @property
    def workspace(self) -> Path:
        """The host side of the mapped folder (``/workspace`` in the sandbox)."""
        return self._workspace


__all__ = ["OUTPUT_DIR", "WindowsSandbox"]
