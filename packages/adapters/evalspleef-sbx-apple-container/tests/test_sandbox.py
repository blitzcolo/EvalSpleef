"""Sandbox-level tests: exec, streaming, timeouts and file transfer."""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

import pytest

from evalspleef.spi.errors import SandboxLost, TransferError
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE


async def test_exit_codes(sandbox) -> None:
    ok = await sandbox.exec(["/bin/sh", "-c", "exit 0"])
    assert ok.exit_code == 0 and not ok.timed_out
    failed = await sandbox.exec(["/bin/sh", "-c", "exit 7"])
    assert failed.exit_code == 7


async def test_stdout_stderr_separation(sandbox) -> None:
    result = await sandbox.exec("printf 'out-line'; printf 'err-line' >&2")
    assert result.exit_code == 0
    assert result.stdout == b"out-line"
    assert result.stderr == b"err-line"


async def test_timeout_kills_process_tree(sandbox) -> None:
    command = "sleep 30 & echo $! > timeout.pid; wait"
    result = await sandbox.exec(command, timeout_s=0.5)
    assert result.timed_out
    assert result.exit_code == TIMEOUT_EXIT_CODE
    pid = (await sandbox.exec(["cat", "timeout.pid"])).stdout.strip().decode()
    assert pid.isdigit()
    gone = await sandbox.exec(
        f'pid={pid}; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done; echo gone',
        timeout_s=5,
    )
    assert gone.exit_code == 0
    assert gone.stdout.strip() == b"gone"


async def test_streaming_callbacks(sandbox) -> None:
    events: list[tuple[str, bytes]] = []
    result = await sandbox.exec(
        "printf a1; printf b1 >&2; printf a2",
        on_output=lambda stream, data: events.append((stream, data)),
    )
    assert result.exit_code == 0
    stdout = b"".join(data for stream, data in events if stream == "stdout")
    stderr = b"".join(data for stream, data in events if stream == "stderr")
    assert stdout == b"a1a2"
    assert stderr == b"b1"
    assert result.stdout == stdout
    assert result.stderr == stderr
    assert not result.truncated


async def test_output_truncation(sandbox) -> None:
    result = await sandbox.exec("head -c 65536 /dev/zero | tr '\\000' x", max_output_bytes=1024)
    assert result.exit_code == 0
    assert result.truncated
    assert 0 < len(result.stdout) <= 1024


async def test_cwd_env_user(sandbox) -> None:
    made = await sandbox.exec(["mkdir", "-p", "subdir"], cwd="/workspace")
    assert made.exit_code == 0
    written = await sandbox.exec(
        ["sh", "-c", "printf cwd-effective > marker.txt"], cwd="/workspace/subdir"
    )
    assert written.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp, "marker.txt")
        await sandbox.download("/workspace/subdir/marker.txt", target)
        assert await asyncio.to_thread(target.read_bytes) == b"cwd-effective"
    env = await sandbox.exec('printf %s "$SPLEEF_TEST_ENV"', env={"SPLEEF_TEST_ENV": "v1"})
    assert env.stdout == b"v1"
    current = await sandbox.exec(["id", "-un"])
    assert current.stdout.strip() == b"agent"
    as_user = await sandbox.exec(["id", "-un"], user="agent")
    assert as_user.stdout.strip() == b"agent"


async def test_spec_env_is_present_in_every_exec(provider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("unit-env", env={"SPLEEF_TASK_ENV": "from-spec"}))
    try:
        result = await sandbox.exec('printf %s "$SPLEEF_TASK_ENV"')
        assert result.stdout == b"from-spec"
    finally:
        await sandbox.destroy()


async def test_concurrent_exec(sandbox) -> None:
    async def one(index: int) -> bytes:
        result = await sandbox.exec(f"printf task-{index}")
        return result.stdout

    outputs = await asyncio.gather(*(one(i) for i in range(4)))
    assert outputs == [f"task-{i}".encode() for i in range(4)]


async def test_stdin(sandbox) -> None:
    result = await sandbox.exec(["cat"], stdin=b"ping")
    assert result.exit_code == 0
    assert result.stdout == b"ping"


async def test_file_roundtrip(sandbox) -> None:
    payload = bytes(range(256)) * 4 + b"\r\n\x00tail"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        source = local / "payload.bin"
        source.write_bytes(payload)
        await sandbox.upload(source, "/workspace/ft/payload.bin")
        target = local / "returned.bin"
        await sandbox.download("/workspace/ft/payload.bin", target)
        assert target.read_bytes() == payload


async def test_directory_roundtrip_preserves_modes(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        tree = local / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "readme.txt").write_bytes(b"alpha\n")
        (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
        (tree / "readme.txt").chmod(0o640)
        (tree / "sub" / "nested.bin").chmod(0o755)
        await sandbox.upload(tree, "/workspace/dirft/tree")
        back = local / "back"
        await sandbox.download("/workspace/dirft/tree", back)
        assert (back / "readme.txt").read_bytes() == b"alpha\n"
        assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
        assert stat.S_IMODE((back / "readme.txt").stat().st_mode) == 0o640
        assert stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode) == 0o755


async def test_download_rejects_escaping_symlink(sandbox) -> None:
    created = await sandbox.exec(["ln", "-sfn", "/etc/passwd", "escape"])
    assert created.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(TransferError):
        await sandbox.download("/workspace/escape", Path(tmp, "escape"))


async def test_download_missing_path_raises(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(TransferError):
        await sandbox.download("/workspace/does-not-exist", Path(tmp, "x"))


async def test_download_size_cap(sandbox) -> None:
    written = await sandbox.exec(["sh", "-c", "printf 0123456789 > big.txt"])
    assert written.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(TransferError):
        await sandbox.download("/workspace/big.txt", Path(tmp, "big.txt"), max_bytes=4)


async def test_upload_rejects_relative_remote_path(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp, "file.txt")
        await asyncio.to_thread(source.write_text, "x", encoding="utf-8")
        with pytest.raises(TransferError):
            await sandbox.upload(source, "relative/path.txt")


async def test_exec_after_destroy_raises(sandbox) -> None:
    await sandbox.destroy()
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_exec_after_external_removal_raises(provider, sandbox) -> None:
    await provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_describe(sandbox) -> None:
    described = await sandbox.describe()
    assert described["container"] == sandbox.id
    assert described["template_digest"] == sandbox.spec.template.digest
    assert described["memory_mb"] == sandbox.spec.resources.memory_mb
