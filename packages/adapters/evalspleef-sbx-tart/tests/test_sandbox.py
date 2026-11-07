"""Sandbox-level tests: exec semantics, timeouts, streaming, files, GUI and SSH."""

from __future__ import annotations

import asyncio
import shlex
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_tart.provider import TartProvider

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import ExecResult, Sandbox, SandboxSpec

SpecFactory = Callable[..., SandboxSpec]
ProviderFactory = Callable[..., TartProvider]


async def test_exec_reports_exit_code_and_separates_streams(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["sh", "-c", "printf out; printf err >&2; exit 7"])
    assert isinstance(result, ExecResult)
    assert result.exit_code == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.timed_out is False
    assert result.truncated is False


async def test_exec_string_uses_shell_and_cwd_and_env(sandbox: Sandbox) -> None:
    made = await sandbox.exec(["mkdir", "-p", "sub/dir"])
    assert made.exit_code == 0, made.stderr
    result = await sandbox.exec(
        'pwd; printf %s "$SPLEEF_TEST"',
        cwd="/workspace/sub/dir",
        env={"SPLEEF_TEST": "env-value"},
    )
    assert result.exit_code == 0, result.stderr
    output = result.stdout.decode().splitlines()
    assert output[0].endswith("sub/dir"), output
    assert output[1] == "env-value"


async def test_exec_stdin_roundtrip(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["cat"], stdin=b"stdin-payload")
    assert result.exit_code == 0
    assert result.stdout == b"stdin-payload"


async def test_timeout_kills_the_whole_process_tree(sandbox: Sandbox) -> None:
    result = await sandbox.exec(
        "sleep 30 & echo $! > tree.pid; wait",
        timeout_s=0.4,
    )
    assert result.timed_out is True
    assert result.exit_code == 124
    pid_result = await sandbox.exec(["cat", "tree.pid"])
    assert pid_result.exit_code == 0, pid_result.stderr
    pid = pid_result.stdout.decode().strip()
    assert pid.isdigit()
    gone = await sandbox.exec(
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.05; done; echo gone",
        timeout_s=15.0,
    )
    assert gone.exit_code == 0, gone.stderr
    assert gone.stdout.strip() == b"gone"


async def test_streaming_callbacks_are_ordered(sandbox: Sandbox) -> None:
    events: list[tuple[str, bytes]] = []
    result = await sandbox.exec(
        "printf a1; printf b1 >&2; printf a2",
        on_output=lambda channel, data: events.append((channel, data)),
    )
    assert result.exit_code == 0
    assert b"".join(data for channel, data in events if channel == "stdout") == b"a1a2"
    assert b"".join(data for channel, data in events if channel == "stderr") == b"b1"


async def test_output_is_truncated_at_the_budget(sandbox: Sandbox) -> None:
    result = await sandbox.exec(
        "head -c 65536 /dev/zero | tr '\\000' x",
        max_output_bytes=1024,
    )
    assert result.exit_code == 0
    assert result.truncated is True
    assert 0 < len(result.stdout) <= 1024


async def test_concurrent_exec_on_one_vm(sandbox: Sandbox) -> None:
    async def one(index: int) -> ExecResult:
        return await sandbox.exec(f"sleep 0.2; printf task-{index}")

    results = await asyncio.gather(*(one(index) for index in range(4)))
    for index, result in enumerate(results):
        assert result.exit_code == 0, result.stderr
        assert result.stdout == f"task-{index}".encode()


async def test_upload_download_file_and_directory(sandbox: Sandbox, tmp_path: Path) -> None:
    payload = bytes(range(256)) * 3 + b"\r\n\x00tail"
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    await sandbox.upload(source, "/workspace/conf/payload.bin")
    returned = tmp_path / "returned.bin"
    await sandbox.download("/workspace/conf/payload.bin", returned)
    assert returned.read_bytes() == payload

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "readme.txt").write_bytes(b"alpha\n")
    (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
    (tree / "readme.txt").chmod(0o640)
    (tree / "sub" / "nested.bin").chmod(0o755)
    await sandbox.upload(tree, "/workspace/conf/tree")
    back = tmp_path / "back"
    await sandbox.download("/workspace/conf/tree", back)
    assert (back / "readme.txt").read_bytes() == b"alpha\n"
    assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
    assert stat.S_IMODE((back / "readme.txt").stat().st_mode) == 0o640
    assert stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode) == 0o755


async def test_download_rejects_an_escaping_symlink(sandbox: Sandbox, tmp_path: Path) -> None:
    created = await sandbox.exec(["ln", "-sfn", "/etc/passwd", "escape-link"])
    assert created.exit_code == 0, created.stderr
    with pytest.raises(TransferError):
        await sandbox.download("/workspace/escape-link", tmp_path / "escape")


async def test_destroy_is_idempotent_and_exec_fails_afterwards(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    sandbox = await provider.create(spec_factory("life-1"))
    await sandbox.destroy()
    await sandbox.destroy()
    assert await provider.list({"trial": "life-1"}) == []
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_describe_reports_the_template_digest(sandbox: Sandbox) -> None:
    described = await sandbox.describe()
    assert described["digest"] == "sha256:" + "a" * 64
    assert described["os"] == "macos"
    assert described["cpus"] == 2
    assert described["memory_mb"] == 4096


async def test_gui_endpoint_answers_with_rfb(
    provider: TartProvider, spec_factory: SpecFactory
) -> None:
    sandbox = await provider.create(spec_factory("gui-1", gui=True))
    try:
        endpoint = await sandbox.gui_endpoint()
        assert endpoint.startswith("vnc://")
        host, _, port = endpoint.removeprefix("vnc://").rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            banner = await asyncio.wait_for(reader.read(64), timeout=5.0)
        finally:
            writer.close()
            await writer.wait_closed()
        assert banner.startswith(b"RFB ")
    finally:
        await sandbox.destroy()


async def test_gui_endpoint_requires_gui_spec(sandbox: Sandbox) -> None:
    with pytest.raises(CapabilityNotSupported):
        await sandbox.gui_endpoint()


class _FakeStdin:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    def write(self, data: bytes) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(data)

    def write_eof(self) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()


class _FakeSSHProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.stdin = _FakeStdin(process)

    def terminate(self) -> None:
        self._process.terminate()

    async def wait(self) -> Any:
        await self._process.wait()
        return _Done(self._process.returncode)


class _Done:
    def __init__(self, code: int | None) -> None:
        self.exit_status = code


class _FakeSSHConnection:
    """An SSH server double: runs the command on the host inside the fake VM."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def create_process(self, command: str, encoding: Any = None) -> _FakeSSHProcess:
        argv = shlex.split(command)
        if argv and argv[0] == "sudo":
            index = argv.index("--") + 1
            argv = argv[index:]
        argv = [token.replace("/workspace", str(self._workspace)) for token in argv]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return _FakeSSHProcess(process)

    def close(self) -> None:
        return

    async def wait_closed(self) -> None:
        return


def _ssh_connector(fake_root: Path) -> Callable[..., Any]:
    async def connect(target: dict[str, Any]) -> _FakeSSHConnection:
        assert target["host"] == "127.0.0.1"
        vms = sorted((fake_root / "vms").iterdir())
        workspace = vms[0] / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return _FakeSSHConnection(workspace)

    return connect


async def test_ssh_transport_executes_commands(
    provider_factory: ProviderFactory,
    fake_tart_root: Path,
    spec_factory: SpecFactory,
) -> None:
    provider = provider_factory(exec_transport="ssh")
    provider.ssh_connector = _ssh_connector(fake_tart_root)
    sandbox = await provider.create(spec_factory("ssh-1"))
    try:
        result = await sandbox.exec(["sh", "-c", "printf ssh-ok; exit 5"])
        assert result.exit_code == 5
        assert result.stdout == b"ssh-ok"
    finally:
        await sandbox.destroy()


async def test_auto_transport_falls_back_to_ssh_when_the_agent_is_down(
    provider: TartProvider,
    fake_tart_root: Path,
    spec_factory: SpecFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider.ssh_connector = _ssh_connector(fake_tart_root)
    sandbox = await provider.create(spec_factory("fallback-1"))
    try:
        monkeypatch.setenv("FAKE_TART_GUEST_AGENT", "down")
        result = await sandbox.exec(["sh", "-c", "printf fallback"])
        assert result.exit_code == 0, result.stderr
        assert result.stdout == b"fallback"
    finally:
        await sandbox.destroy()


async def test_probe_script_runs_through_exec(
    sandbox: Sandbox,
) -> None:
    from evalspleef_sbx_tart.isolation import run_isolation_probes

    from evalspleef.spi.caps import OSFamily

    exit_code, stdout = await run_isolation_probes(sandbox, OSFamily.MACOS)
    text = stdout.decode()
    assert exit_code in (0, 1), text
    assert "CHECK non-root" in text
    assert "CHECK no-sudo" in text
    assert "CHECK offline-test" in text
    assert "RESULT " in text
