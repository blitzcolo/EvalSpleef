"""Sandbox exec, streaming, timeouts and file transfer against FakeKubeApi."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest
from evalspleef_sbx_k8s_agent_sandbox.provider import K8sAgentSandboxProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SandboxLost, TransferError
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

_TEMPLATE_REF = "ghcr.io/evalspleef/test-sandbox:latest"
_TEMPLATE_DIGEST = "sha256:" + "0" * 64


def build_spec(trial_id: str, *, network: NetMode = NetMode.OFF) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref=_TEMPLATE_REF, digest=_TEMPLATE_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=10240),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=600,
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "adapter-test",
        },
    )


async def test_exit_code_stdout_and_stderr(sandbox) -> None:
    result = await sandbox.exec("printf 'out'; printf 'err' >&2; exit 7")
    assert result.exit_code == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.timed_out is False


async def test_list_command_never_uses_a_shell(sandbox) -> None:
    result = await sandbox.exec(["printf", "plain"])
    assert result.exit_code == 0
    assert result.stdout == b"plain"


async def test_stdin_is_forwarded(sandbox) -> None:
    result = await sandbox.exec(["cat"], stdin=b"fed-in")
    assert result.exit_code == 0
    assert result.stdout == b"fed-in"


async def test_streaming_callbacks_are_ordered(sandbox) -> None:
    events: list[tuple[str, bytes]] = []
    result = await sandbox.exec(
        "printf 'a1'; printf 'b1' >&2; printf 'a2'",
        on_output=lambda stream, data: events.append((stream, data)),
    )
    assert result.exit_code == 0
    assert b"".join(data for name, data in events if name == "stdout") == b"a1a2"
    assert b"".join(data for name, data in events if name == "stderr") == b"b1"
    assert result.stdout == b"a1a2"


async def test_output_above_the_budget_is_truncated(sandbox) -> None:
    result = await sandbox.exec(
        "head -c 65536 /dev/zero | tr '\\000' x",
        max_output_bytes=1024,
    )
    assert result.exit_code == 0
    assert result.truncated is True
    assert len(result.stdout) <= 1024


async def test_timeout_kills_the_process_tree(sandbox) -> None:
    result = await sandbox.exec(
        "sleep 30 & echo $! > child.pid; wait",
        timeout_s=0.3,
    )
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    child = await sandbox.exec(["cat", "child.pid"])
    assert child.exit_code == 0
    pid = child.stdout.decode().strip()
    assert pid.isdigit()
    probe = await sandbox.exec(
        'pid="$(cat child.pid)"; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done; echo gone',
        timeout_s=5.0,
    )
    assert probe.exit_code == 0, probe.stderr
    assert probe.stdout.strip() == b"gone"


async def test_cwd_and_env_take_effect(sandbox) -> None:
    made = await sandbox.exec(["mkdir", "-p", "sub"])
    assert made.exit_code == 0
    written = await sandbox.exec(
        ["sh", "-c", "printf 'cwd-effective' > marker.txt"],
        cwd="/workspace/sub",
    )
    assert written.exit_code == 0
    probe = await sandbox.exec(["cat", "marker.txt"], cwd="/workspace/sub")
    assert probe.stdout == b"cwd-effective"
    env_result = await sandbox.exec('printf %s "$SPLEEF_CONF_ENV"', env={"SPLEEF_CONF_ENV": "x"})
    assert env_result.stdout == b"x"


async def test_concurrent_exec_calls(sandbox) -> None:
    async def one(index: int) -> bytes:
        result = await sandbox.exec(f"sleep 0.1; printf 'task-{index}'")
        assert result.exit_code == 0
        return result.stdout

    assert await asyncio.gather(*(one(index) for index in range(4))) == [
        b"task-0",
        b"task-1",
        b"task-2",
        b"task-3",
    ]


async def test_file_and_directory_round_trip(sandbox, tmp_path: Path) -> None:
    payload = bytes(range(256)) * 2 + b"\x00tail"
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    await sandbox.upload(source, "/workspace/files/payload.bin")
    returned = tmp_path / "returned.bin"
    await sandbox.download("/workspace/files/payload.bin", returned)
    assert returned.read_bytes() == payload

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "readme.txt").write_bytes(b"alpha\n")
    (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01")
    os.chmod(tree / "readme.txt", 0o640)
    os.chmod(tree / "sub" / "nested.bin", 0o755)
    await sandbox.upload(tree, "/workspace/files/tree")
    back = tmp_path / "back"
    await sandbox.download("/workspace/files/tree", back)
    assert (back / "readme.txt").read_bytes() == b"alpha\n"
    assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01"
    assert stat.S_IMODE((back / "readme.txt").stat().st_mode) == 0o640
    assert stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode) == 0o755


async def test_download_rejects_an_escaping_symlink(sandbox, tmp_path: Path) -> None:
    made = await sandbox.exec(["ln", "-sfn", "/etc/passwd", "escape"])
    assert made.exit_code == 0
    with pytest.raises(TransferError):
        await sandbox.download("/workspace/escape", tmp_path / "escape")


async def test_upload_rejects_a_local_symlink_escape(sandbox, tmp_path: Path) -> None:
    link = tmp_path / "outside"
    link.symlink_to("/etc/passwd")
    with pytest.raises(TransferError):
        await sandbox.upload(link, "/workspace/outside")


async def test_destroy_is_idempotent(provider: K8sAgentSandboxProvider) -> None:
    instance = await provider.create(build_spec("destroy-1"))
    await instance.destroy()
    await instance.destroy()
    assert await provider.list({"trial": "destroy-1"}) == []


async def test_use_after_loss_raises_sandbox_lost(
    provider: K8sAgentSandboxProvider,
) -> None:
    instance = await provider.create(build_spec("lost-1"))
    await provider.lose(instance.id)
    with pytest.raises(SandboxLost):
        await instance.exec(["true"])
    await instance.destroy()
