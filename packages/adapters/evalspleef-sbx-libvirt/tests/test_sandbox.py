"""Sandbox-level tests: exec streaming, timeouts, stdin, SFTP and GUI plumbing."""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE


async def test_exit_codes(sandbox) -> None:
    ok = await sandbox.exec(["sh", "-c", "exit 0"])
    assert ok.exit_code == 0 and not ok.timed_out
    failed = await sandbox.exec(["sh", "-c", "exit 7"])
    assert failed.exit_code == 7


async def test_stdout_stderr_separation(sandbox) -> None:
    result = await sandbox.exec("printf 'out-line'; printf 'err-line' >&2")
    assert result.exit_code == 0
    assert result.stdout == b"out-line"
    assert result.stderr == b"err-line"


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


async def test_timeout_kills_process_tree(sandbox) -> None:
    result = await sandbox.exec("sleep 30 & echo $! > timeout.pid; wait", timeout_s=0.5)
    assert result.timed_out
    assert result.exit_code == TIMEOUT_EXIT_CODE
    pid_result = await sandbox.exec(["cat", "timeout.pid"])
    pid = pid_result.stdout.strip().decode()
    assert pid.isdigit()
    gone = await sandbox.exec(
        f'pid="{pid}"; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done; echo gone',
        timeout_s=5,
    )
    assert gone.exit_code == 0
    assert gone.stdout.strip() == b"gone"


async def test_stdin(sandbox) -> None:
    result = await sandbox.exec(["cat"], stdin=b"ping")
    assert result.exit_code == 0
    assert result.stdout == b"ping"


async def test_default_cwd_is_workspace(sandbox) -> None:
    written = await sandbox.exec(["sh", "-c", "printf workspace > relative.txt"])
    assert written.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "relative.txt"
        await sandbox.download("/workspace/relative.txt", target)
        assert target.read_bytes() == b"workspace"


async def test_cwd_and_env(sandbox) -> None:
    made = await sandbox.exec(["mkdir", "-p", "subdir"])
    assert made.exit_code == 0
    written = await sandbox.exec(
        ["sh", "-c", "printf cwd-effective > marker.txt"], cwd="/workspace/subdir"
    )
    assert written.exit_code == 0
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "marker.txt"
        await sandbox.download("/workspace/subdir/marker.txt", target)
        assert target.read_bytes() == b"cwd-effective"
    env = await sandbox.exec('printf %s "$SPLEEF_TEST_ENV"', env={"SPLEEF_TEST_ENV": "v1"})
    assert env.stdout == b"v1"


async def test_exec_as_other_user_is_rejected(sandbox) -> None:
    with pytest.raises(CapabilityNotSupported):
        await sandbox.exec(["id", "-un"], user="root")


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


async def test_upload_rejects_symlink(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        (local / "target.txt").write_bytes(b"data")
        link = local / "link.txt"
        link.symlink_to(local / "target.txt")
        with pytest.raises(TransferError):
            await sandbox.upload(link, "/workspace/link.txt")


async def test_download_enforces_max_bytes(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        source = local / "large.bin"
        source.write_bytes(b"x" * 2048)
        await sandbox.upload(source, "/workspace/large.bin")
        with pytest.raises(TransferError):
            await sandbox.download("/workspace/large.bin", local / "returned.bin", max_bytes=1024)


async def test_download_missing_path_raises(sandbox) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(TransferError):
        await sandbox.download("/workspace/does-not-exist", Path(tmp, "x"))


async def test_exec_after_destroy_raises(sandbox) -> None:
    await sandbox.destroy()
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_gui_endpoint_answers_vnc(sandbox) -> None:
    endpoint = await sandbox.gui_endpoint()
    parts = urlsplit(endpoint)
    assert parts.scheme == "vnc"
    assert parts.hostname is not None and parts.port is not None
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    try:
        banner = await reader.read(64)
    finally:
        writer.close()
        await writer.wait_closed()
    assert banner.startswith(b"RFB ")


async def test_describe(sandbox) -> None:
    info = await sandbox.describe()
    assert info["domain"] == sandbox.id
    assert info["host"] == "127.0.0.1"
    assert info["template_digest"] == sandbox.spec.template.digest
