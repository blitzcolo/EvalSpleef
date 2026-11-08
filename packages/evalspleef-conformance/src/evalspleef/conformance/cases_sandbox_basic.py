"""Sandbox conformance cases: lifecycle, exec and file transfer (TECH-DESIGN section 15.1).

The cases check the mandatory sandbox SPI surface (create / exec / upload / download /
destroy) plus the declared optional capabilities they depend on. A case never passes
vacuously: either its ``applies`` callable names why it cannot run on a target, or ``run``
raises ``AssertionError`` with the observed values.

Environment overrides:

``SPLEEF_CONFORMANCE_LARGE_FILE_BYTES`` caps the payload size used by C-FILE-03 (default
100 MB). Slow test doubles can lower it without changing the case, for example
``SPLEEF_CONFORMANCE_LARGE_FILE_BYTES=1048576`` for a one-megabyte round trip.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from evalspleef.conformance.case import Case
from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import OSFamily
from evalspleef.spi.errors import SandboxLost, SpleefError, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)

LARGE_FILE_BYTES_ENV = "SPLEEF_CONFORMANCE_LARGE_FILE_BYTES"
LARGE_FILE_BYTES_DEFAULT = 100 * 1024 * 1024
"""C-FILE-03 payload size in bytes when ``LARGE_FILE_BYTES_ENV`` is not set."""

_EXEC_POLL_DEADLINE_S = 5.0


def _provider(target: ConformanceTarget) -> SandboxProvider:
    """The sandbox provider under test, with a clear error for a model target."""
    if target.kind != "sandbox" or not isinstance(target.provider, SandboxProvider):
        raise TypeError(f"target {target.name!r} is not a sandbox target")
    return target.provider


def _capabilities(target: ConformanceTarget) -> SandboxCapabilities:
    return _provider(target).capabilities()


def _is_posix(spec: SandboxSpec) -> bool:
    return spec.os in (OSFamily.LINUX, OSFamily.MACOS)


def _command(sandbox: Sandbox, posix: str, windows: str) -> str:
    """Pick the command variant for the sandbox's OS family."""
    return posix if _is_posix(sandbox.spec) else windows


def _ids(entries: list[dict[str, Any]]) -> set[str]:
    return {str(entry["id"]) for entry in entries}


def _text(result: ExecResult) -> str:
    return result.stdout.decode("utf-8", errors="replace")


def _large_file_bytes() -> int:
    raw = os.environ.get(LARGE_FILE_BYTES_ENV)
    if raw is None or raw == "":
        return LARGE_FILE_BYTES_DEFAULT
    try:
        value = int(raw)
    except ValueError as exc:
        raise AssertionError(
            f"{LARGE_FILE_BYTES_ENV} must be an integer number of bytes, got {raw!r}"
        ) from exc
    if value <= 0:
        raise AssertionError(f"{LARGE_FILE_BYTES_ENV} must be positive, got {value}")
    return value


# -- lifecycle ---------------------------------------------------------------


async def _life_create_exec_destroy(target: ConformanceTarget) -> None:
    provider = _provider(target)
    async with target.open_sandbox("conf-life-01") as sandbox:
        result = await sandbox.exec(["echo", "life-01"])
        assert result.exit_code == 0, (
            f"exec after create failed: exit {result.exit_code}, stderr {result.stderr!r}"
        )
        assert result.stdout.strip() == b"life-01", f"unexpected stdout {result.stdout!r}"
        labels = dict(sandbox.spec.labels)
        listed = await provider.list(labels)
        assert sandbox.id in _ids(listed), "a live sandbox is not visible through its labels"
    listed_after = await provider.list(labels)
    assert sandbox.id not in _ids(listed_after), "destroyed sandbox is still listed"


async def _life_destroy_is_idempotent(target: ConformanceTarget) -> None:
    provider = _provider(target)
    async with target.open_sandbox("conf-life-02") as sandbox:
        await sandbox.destroy()
        await sandbox.destroy()  # a second destroy must succeed
        labels = dict(sandbox.spec.labels)
        listed = await provider.list(labels)
        assert sandbox.id not in _ids(listed), "destroyed sandbox is still listed"


async def _life_lifetime_expiry(target: ConformanceTarget) -> None:
    injector = target.injectors.get("expire_lifetime")
    assert injector is not None, (
        "case C-LIFE-03 applies, so the target must provide an 'expire_lifetime' injector"
    )
    provider = _provider(target)
    async with target.open_sandbox("conf-life-03") as sandbox:
        await injector()
        labels = dict(sandbox.spec.labels)
        expired: SpleefError | None = None
        result: ExecResult | None = None
        try:
            result = await sandbox.exec(["echo", "still-alive"])
        except SpleefError as exc:
            expired = exc
        if result is not None:
            listed = await provider.list(labels)
            assert sandbox.id not in _ids(listed), (
                "sandbox exceeded max_lifetime_s but still executes commands"
            )
        else:
            assert isinstance(expired, SandboxLost), (
                f"expected SandboxLost after lifetime expiry, got {expired!r}"
            )
        # No reap here: whatever reclaims an expired sandbox must be the adapter's own TTL
        # path (vendor timeout, hostd timer, sweep), not this check.
        listed_after = await provider.list(labels)
        assert sandbox.id not in _ids(listed_after), "expired sandbox was not reclaimed"


async def _life_labels_reap(target: ConformanceTarget) -> None:
    provider = _provider(target)
    async with target.open_sandbox("conf-life-04") as sandbox:
        labels = dict(sandbox.spec.labels)
        listed = await provider.list(labels)
        assert sandbox.id in _ids(listed), "list(labels) does not return the created sandbox"
        await provider.reap(labels, older_than_s=0)
        listed_after = await provider.list(labels)
        assert sandbox.id not in _ids(listed_after), "reap(labels) did not reclaim the sandbox"


# -- exec --------------------------------------------------------------------


async def _exec_exit_code(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-01") as sandbox:
        shell = ["sh", "-c"] if _is_posix(sandbox.spec) else ["cmd", "/c"]
        ok = await sandbox.exec([*shell, "exit 0"])
        assert ok.exit_code == 0, f"successful command reported exit {ok.exit_code}"
        assert not ok.timed_out, "a fast command was reported as timed out"
        failed = await sandbox.exec([*shell, "exit 7"])
        assert failed.exit_code == 7, f"exit 7 command reported exit {failed.exit_code}"


async def _exec_stream_separation(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-02") as sandbox:
        command = _command(
            sandbox,
            "printf 'out-line'; printf 'err-line' >&2",
            "Write-Output -NoNewline 'out-line'; [Console]::Error.Write('err-line')",
        )
        result = await sandbox.exec(command)
        assert result.exit_code == 0, f"command failed: {result.stderr!r}"
        assert result.stdout == b"out-line", f"stdout {result.stdout!r}"
        assert result.stderr == b"err-line", f"stderr {result.stderr!r}"


async def _exec_timeout_kills_tree(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-03") as sandbox:
        pid_file = "conf-exec-03.pid"
        command = _command(
            sandbox,
            f"sleep 30 & echo $! > {pid_file}; wait",
            "Start-Sleep -Seconds 30",
        )
        result = await sandbox.exec(command, timeout_s=0.5)
        assert result.timed_out, "exec did not report a timeout"
        assert result.exit_code == TIMEOUT_EXIT_CODE, (
            f"timeout exit code {result.exit_code}, expected {TIMEOUT_EXIT_CODE}"
        )
        if not _is_posix(sandbox.spec):
            return
        pid_result = await sandbox.exec(["cat", pid_file])
        assert pid_result.exit_code == 0, (
            f"background child pid was not recorded: {pid_result.stderr!r}"
        )
        pid = _text(pid_result).strip()
        assert pid.isdigit(), f"unexpected pid contents {pid_result.stdout!r}"
        wait = (
            f'pid="$(cat {pid_file})"; '
            'while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done; echo gone'
        )
        gone = await sandbox.exec(wait, timeout_s=_EXEC_POLL_DEADLINE_S)
        assert gone.exit_code == 0, (
            f"background child {pid} survived the timeout kill: {gone.stderr!r}"
        )
        assert _text(gone).strip() == "gone", f"unexpected wait output {gone.stdout!r}"


async def _exec_streaming(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-04") as sandbox:
        events: list[tuple[str, bytes]] = []

        def on_output(stream: str, data: bytes) -> None:
            events.append((stream, data))

        callback: OutputCallback = on_output
        command = _command(
            sandbox,
            "printf 'a1'; printf 'b1' >&2; printf 'a2'",
            "Write-Output -NoNewline 'a1'; [Console]::Error.Write('b1'); "
            "Write-Output -NoNewline 'a2'",
        )
        result = await sandbox.exec(command, on_output=callback)
        assert result.exit_code == 0, f"command failed: {result.stderr!r}"
        assert events, "a streaming target delivered no on_output callbacks"
        streamed_stdout = b"".join(data for name, data in events if name == "stdout")
        streamed_stderr = b"".join(data for name, data in events if name == "stderr")
        assert streamed_stdout == b"a1a2", f"streamed stdout {streamed_stdout!r}"
        assert streamed_stderr == b"b1", f"streamed stderr {streamed_stderr!r}"
        assert result.stdout == streamed_stdout, (
            f"buffered stdout {result.stdout!r} differs from streamed {streamed_stdout!r}"
        )
        assert result.stderr == streamed_stderr, (
            f"buffered stderr {result.stderr!r} differs from streamed {streamed_stderr!r}"
        )
        assert not result.truncated, "a small streamed command was reported truncated"


async def _exec_output_truncation(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-05") as sandbox:
        budget = 1024
        command = _command(
            sandbox,
            "head -c 65536 /dev/zero | tr '\\000' x",
            "$s = 'x' * 65536; [Console]::Out.Write($s)",
        )
        result = await sandbox.exec(command, max_output_bytes=budget)
        assert result.exit_code == 0, f"producer failed: {result.stderr!r}"
        assert result.truncated, "output above max_output_bytes was not flagged truncated"
        assert result.stdout, "truncated stdout was dropped entirely"
        assert len(result.stdout) <= budget, (
            f"truncated stdout is {len(result.stdout)} bytes, budget {budget}"
        )


async def _exec_cwd_env_user(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-06") as sandbox:
        # Paths are relative to the sandbox workspace, the one fixed by the SPI.
        subdir = "conf-exec-06"
        created = await sandbox.exec(
            ["mkdir", "-p", subdir] if _is_posix(sandbox.spec) else ["cmd", "/c", f"mkdir {subdir}"]
        )
        assert created.exit_code == 0, f"could not create cwd: {created.stderr!r}"

        marker = b"cwd-effective"
        written = await sandbox.exec(
            ["sh", "-c", f"printf '{marker.decode()}' > marker.txt"],
            cwd=f"/workspace/{subdir}",
        )
        assert written.exit_code == 0, f"could not write in cwd: {written.stderr!r}"
        with tempfile.TemporaryDirectory() as tmp:
            marker_copy = Path(tmp) / "marker.txt"
            await sandbox.download(f"/workspace/{subdir}/marker.txt", marker_copy)
            assert marker_copy.read_bytes() == marker, "cwd did not take effect"

        env_command = _command(
            sandbox,
            'printf %s "$SPLEEF_CONF_ENV"',
            "[Console]::Out.Write($env:SPLEEF_CONF_ENV)",
        )
        env_result = await sandbox.exec(env_command, env={"SPLEEF_CONF_ENV": "conf-value"})
        assert env_result.exit_code == 0, f"env command failed: {env_result.stderr!r}"
        assert _text(env_result) == "conf-value", f"env is {_text(env_result)!r}"

        if not _is_posix(sandbox.spec):
            return
        expected_user = sandbox.spec.run_as_user
        current = await sandbox.exec(["id", "-un"])
        assert current.exit_code == 0, f"id -un failed: {current.stderr!r}"
        if _text(current).strip() == expected_user:
            return
        account = await sandbox.exec(["id", "-u", expected_user])
        if account.exit_code != 0:
            # The requested account is absent from this image; switching cannot be exercised.
            return
        switched = await sandbox.exec(["id", "-un"], user=expected_user)
        assert switched.exit_code == 0, f"exec as {expected_user!r} failed: {switched.stderr!r}"
        assert _text(switched).strip() == expected_user, (
            f"exec user={expected_user!r} ran as {_text(switched).strip()!r}"
        )


async def _exec_concurrent(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-exec-07") as sandbox:

        async def one(index: int) -> tuple[int, ExecResult]:
            token = f"task-{index}"
            command = _command(
                sandbox,
                f"sleep 0.2; printf '{token}'",
                f"Start-Sleep -Milliseconds 200; Write-Output -NoNewline '{token}'",
            )
            return index, await sandbox.exec(command)

        results = await asyncio.gather(*(one(index) for index in range(4)))
        for index, result in results:
            assert result.exit_code == 0, f"concurrent exec {index} failed: {result.stderr!r}"
            assert result.stdout == f"task-{index}".encode(), (
                f"concurrent exec {index} returned {result.stdout!r}"
            )


# -- file transfer -----------------------------------------------------------


async def _file_binary_roundtrip(target: ConformanceTarget) -> None:
    payload = bytes(range(256)) * 4 + b"\r\n\x00tail"
    text = b"line-1\nline-2\n"
    async with target.open_sandbox("conf-file-01") as sandbox:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            binary_src = local / "payload.bin"
            binary_src.write_bytes(payload)
            text_src = local / "payload.txt"
            text_src.write_bytes(text)
            await sandbox.upload(binary_src, "/workspace/conf-file-01/payload.bin")
            await sandbox.upload(text_src, "/workspace/conf-file-01/payload.txt")
            binary_dst = local / "returned.bin"
            text_dst = local / "returned.txt"
            await sandbox.download("/workspace/conf-file-01/payload.bin", binary_dst)
            await sandbox.download("/workspace/conf-file-01/payload.txt", text_dst)
            assert binary_dst.read_bytes() == payload, "binary file changed across the round trip"
            assert text_dst.read_bytes() == text, "text file changed across the round trip"


async def _file_directory_roundtrip(target: ConformanceTarget) -> None:
    async with target.open_sandbox("conf-file-02") as sandbox:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            tree = local / "tree"
            (tree / "sub").mkdir(parents=True)
            (tree / "readme.txt").write_bytes(b"alpha\n")
            (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
            os.chmod(tree / "readme.txt", 0o640)
            os.chmod(tree / "sub" / "nested.bin", 0o755)
            await sandbox.upload(tree, "/workspace/conf-file-02/tree")
            back = local / "back"
            await sandbox.download("/workspace/conf-file-02/tree", back)
            assert (back / "readme.txt").read_bytes() == b"alpha\n"
            assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
            if _is_posix(sandbox.spec):
                readme_mode = stat.S_IMODE((back / "readme.txt").stat().st_mode)
                nested_mode = stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode)
                assert readme_mode == 0o640, f"readme.txt mode {oct(readme_mode)}"
                assert nested_mode == 0o755, f"nested.bin mode {oct(nested_mode)}"


def _write_sized_file(path: Path, size: int) -> None:
    chunk = bytes(range(256)) * 4096  # 1 MiB
    remaining = size
    with path.open("wb") as handle:
        while remaining > 0:
            piece = chunk if remaining >= len(chunk) else chunk[:remaining]
            handle.write(piece)
            remaining -= len(piece)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _file_large(target: ConformanceTarget) -> None:
    size = _large_file_bytes()
    async with target.open_sandbox("conf-file-03") as sandbox:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            source = local / "large.bin"
            _write_sized_file(source, size)
            await sandbox.upload(source, "/workspace/conf-file-03/large.bin")
            returned = local / "large-returned.bin"
            await sandbox.download("/workspace/conf-file-03/large.bin", returned)
            assert returned.stat().st_size == size, (
                f"downloaded {returned.stat().st_size} bytes, expected {size}"
            )
            assert _sha256(returned) == _sha256(source), "large file content changed"


async def _file_reject_escaping_symlink(target: ConformanceTarget) -> None:
    link = "conf-file-04-escape"
    async with target.open_sandbox("conf-file-04") as sandbox:
        if _is_posix(sandbox.spec):
            created = await sandbox.exec(["ln", "-sfn", "/etc/passwd", link])
        else:
            created = await sandbox.exec(
                [
                    "cmd",
                    "/c",
                    f"mklink {link.replace('/', chr(92))} C:\\Windows\\win.ini",
                ]
            )
        assert created.exit_code == 0, f"could not create the escaping symlink: {created.stderr!r}"
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "escape"
            try:
                await sandbox.download(f"/workspace/{link}", destination)
            except TransferError:
                return
            raise AssertionError("download of a symlink pointing outside the sandbox was accepted")


# -- case table --------------------------------------------------------------


def _requires_streaming(target: ConformanceTarget) -> str | None:
    if not _capabilities(target).streaming:
        return "streaming output not declared"
    return None


def _requires_lifetime_injector(target: ConformanceTarget) -> str | None:
    if "expire_lifetime" not in target.injectors:
        return "target supplies no expire_lifetime fault injector"
    return None


def _always(target: ConformanceTarget) -> str | None:
    return None


CASES: tuple[Case, ...] = (
    Case(
        id="C-LIFE-01",
        kind="sandbox",
        title="create, exec and destroy",
        applies=_always,
        run=_life_create_exec_destroy,
    ),
    Case(
        id="C-LIFE-02",
        kind="sandbox",
        title="destroy is idempotent",
        applies=_always,
        run=_life_destroy_is_idempotent,
    ),
    Case(
        id="C-LIFE-03",
        kind="sandbox",
        title="the sandbox is reclaimed after max_lifetime_s",
        applies=_requires_lifetime_injector,
        run=_life_lifetime_expiry,
    ),
    Case(
        id="C-LIFE-04",
        kind="sandbox",
        title="labels are searchable through list and reap",
        applies=_always,
        run=_life_labels_reap,
    ),
    Case(
        id="C-EXEC-01",
        kind="sandbox",
        title="exit code is reported verbatim",
        applies=_always,
        run=_exec_exit_code,
    ),
    Case(
        id="C-EXEC-02",
        kind="sandbox",
        title="stdout and stderr stay separate",
        applies=_always,
        run=_exec_stream_separation,
    ),
    Case(
        id="C-EXEC-03",
        kind="sandbox",
        title="timeout kills the whole process tree",
        applies=_always,
        run=_exec_timeout_kills_tree,
    ),
    Case(
        id="C-EXEC-04",
        kind="sandbox",
        title="streaming callbacks are ordered and complete",
        applies=_requires_streaming,
        run=_exec_streaming,
    ),
    Case(
        id="C-EXEC-05",
        kind="sandbox",
        title="oversized output is truncated",
        applies=_always,
        run=_exec_output_truncation,
    ),
    Case(
        id="C-EXEC-06",
        kind="sandbox",
        title="cwd, env and user take effect",
        applies=_always,
        run=_exec_cwd_env_user,
    ),
    Case(
        id="C-EXEC-07",
        kind="sandbox",
        title="concurrent exec calls on one sandbox",
        applies=_always,
        run=_exec_concurrent,
    ),
    Case(
        id="C-FILE-01",
        kind="sandbox",
        title="text and binary files round-trip",
        applies=_always,
        run=_file_binary_roundtrip,
    ),
    Case(
        id="C-FILE-02",
        kind="sandbox",
        title="directories round-trip with permission bits",
        applies=_always,
        run=_file_directory_roundtrip,
    ),
    Case(
        id="C-FILE-03",
        kind="sandbox",
        title="large file round-trip",
        applies=_always,
        run=_file_large,
    ),
    Case(
        id="C-FILE-04",
        kind="sandbox",
        title="download rejects a symlink pointing outside the sandbox",
        applies=_always,
        run=_file_reject_escaping_symlink,
    ),
)
