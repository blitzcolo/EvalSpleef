"""Sandbox instance tests against the fake ``wsb`` executable (not a real host)."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest
from evalspleef_sbx_windows_sandbox.provider import WindowsSandboxProvider
from evalspleef_sbx_windows_sandbox.sandbox import WindowsSandbox

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE, ExecResult


async def test_exec_reports_stdout_and_exit_code(sandbox: WindowsSandbox) -> None:
    result = await sandbox.exec(["echo", "hello"])
    assert result.exit_code == 0
    assert result.stdout == b"hello\n"
    assert result.stderr == b""
    assert not result.timed_out and not result.truncated
    failed = await sandbox.exec("exit 7")
    assert failed.exit_code == 7
    assert not failed.timed_out


async def test_exec_keeps_streams_separate(sandbox: WindowsSandbox) -> None:
    result = await sandbox.exec("printf 'out-line'; printf 'err-line' >&2")
    assert result.exit_code == 0
    assert result.stdout == b"out-line"
    assert result.stderr == b"err-line"


async def test_exec_cwd_and_env(sandbox: WindowsSandbox) -> None:
    created = await sandbox.exec(["mkdir", "-p", "nested"])
    assert created.exit_code == 0
    written = await sandbox.exec(
        "printf 'marker' > marker.txt", cwd="/workspace/nested", env={"GREETING": "conf"}
    )
    assert written.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "marker.txt"
        await sandbox.download("/workspace/nested/marker.txt", target)
        assert target.read_bytes() == b"marker"
    env_result = await sandbox.exec('printf %s "$GREETING"', env={"GREETING": "conf-value"})
    assert env_result.stdout == b"conf-value"


async def test_timeout_kills_the_process_tree(sandbox: WindowsSandbox) -> None:
    result = await sandbox.exec("sleep 30 & echo $! > sleepy.pid; wait", timeout_s=0.5)
    assert result.timed_out
    assert result.exit_code == TIMEOUT_EXIT_CODE
    pid_result = await sandbox.exec(["cat", "sleepy.pid"])
    assert pid_result.exit_code == 0
    pid = pid_result.stdout.decode().strip()
    assert pid.isdigit(), pid_result.stdout
    gone = await sandbox.exec(
        f'pid="{pid}"; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done; echo gone',
        timeout_s=5.0,
    )
    assert gone.exit_code == 0
    assert gone.stdout.strip() == b"gone"


async def test_output_truncation(sandbox: WindowsSandbox) -> None:
    result = await sandbox.exec("head -c 8192 /dev/zero | tr '\\000' x", max_output_bytes=256)
    assert result.exit_code == 0
    assert result.truncated
    assert result.stdout
    assert len(result.stdout) <= 256


async def test_on_output_fires_once_at_the_end(sandbox: WindowsSandbox) -> None:
    events: list[tuple[str, bytes]] = []
    result = await sandbox.exec(
        "printf 'a1'; printf 'b1' >&2",
        on_output=lambda stream, data: events.append((stream, data)),
    )
    assert result.stdout == b"a1" and result.stderr == b"b1"
    assert events == [("stdout", b"a1"), ("stderr", b"b1")]


async def test_file_and_directory_roundtrip(sandbox: WindowsSandbox) -> None:
    payload = bytes(range(256)) * 8
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "payload.bin"
        source.write_bytes(payload)
        await sandbox.upload(source, "/workspace/files/payload.bin")
        returned = root / "returned.bin"
        await sandbox.download("/workspace/files/payload.bin", returned)
        assert returned.read_bytes() == payload

        tree = root / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "readme.txt").write_bytes(b"alpha\n")
        (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
        os.chmod(tree / "readme.txt", 0o640)
        os.chmod(tree / "sub" / "nested.bin", 0o755)
        await sandbox.upload(tree, "/workspace/files/tree")
        back = root / "back"
        await sandbox.download("/workspace/files/tree", back)
        assert (back / "readme.txt").read_bytes() == b"alpha\n"
        assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
        assert stat.S_IMODE((back / "readme.txt").stat().st_mode) == 0o640
        assert stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode) == 0o755


async def test_download_rejects_escaping_symlink(sandbox: WindowsSandbox) -> None:
    created = await sandbox.exec(["ln", "-sfn", "/etc/passwd", "escape"])
    assert created.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(TransferError):
        await sandbox.download("/workspace/escape", Path(tmp) / "escape")


async def test_paths_outside_workspace_are_rejected(sandbox: WindowsSandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "x.bin"
        source.write_bytes(b"x")
        for remote in ("/etc/passwd", "relative/path", "/workspace/../escape"):
            with pytest.raises(TransferError):
                await sandbox.upload(source, remote)
            with pytest.raises(TransferError):
                await sandbox.download(remote, Path(tmp) / "out")
        with pytest.raises(TransferError):
            await sandbox.download("/workspace/missing.txt", Path(tmp) / "missing.txt")


async def test_stdin_and_user_are_rejected(sandbox: WindowsSandbox) -> None:
    with pytest.raises(CapabilityNotSupported):
        await sandbox.exec(["cat"], stdin=b"data")
    with pytest.raises(CapabilityNotSupported):
        await sandbox.exec(["id"], user="root")


async def test_destroy_is_idempotent_and_reports_lost(
    provider: WindowsSandboxProvider, sandbox: WindowsSandbox
) -> None:
    await sandbox.destroy()
    await sandbox.destroy()
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    assert await provider.list({}) == []


async def test_describe_reports_guest_and_digest(sandbox: WindowsSandbox) -> None:
    described = await sandbox.describe()
    assert described["guest_os"] == "linux"
    assert described["template_digest"] == "sha256:" + "0" * 64
    assert described["host"] == "windows-sandbox"


async def test_exec_result_has_bytes_streams(sandbox: WindowsSandbox) -> None:
    result: ExecResult = await sandbox.exec("printf '\\377'")
    assert result.stdout == b"\xff"
