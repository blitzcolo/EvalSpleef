"""Exec semantics of the E2B-family base class, run through the fake SDK."""

from __future__ import annotations

import asyncio

import pytest
from evalspleef_sbx_e2b_family.base import LIFETIME_GRACE_S

from evalspleef.spi.errors import SandboxLost
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE

from .conftest import make_spec


async def test_create_sets_timeout_and_metadata(novita_provider, novita_cluster) -> None:
    spec = make_spec("t-timeout", labels={"batch": "b1", "trial": "t-timeout"})
    sandbox = await novita_provider.create(spec)
    try:
        call = novita_cluster.create_calls[-1]
        assert call["timeout"] == spec.max_lifetime_s + LIFETIME_GRACE_S
        assert call["metadata"] == {"batch": "b1", "trial": "t-timeout"}
        assert call["allow_internet_access"] is False  # NetMode.OFF
        assert call["network"] is None
    finally:
        await sandbox.destroy()


async def test_echo_and_exit_codes(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        ok = await sandbox.exec(["echo", "hello"])
        assert ok.exit_code == 0
        assert ok.stdout == b"hello\n"
        assert not ok.timed_out
        failed = await sandbox.exec(["sh", "-c", "exit 7"])
        assert failed.exit_code == 7
        assert not failed.timed_out
    finally:
        await sandbox.destroy()


async def test_stdout_stderr_separation(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        result = await sandbox.exec("printf out-line; printf err-line >&2")
        assert result.exit_code == 0
        assert result.stdout == b"out-line"
        assert result.stderr == b"err-line"
    finally:
        await sandbox.destroy()


async def test_streaming_callbacks(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        events: list[tuple[str, bytes]] = []

        def on_output(name: str, data: bytes) -> None:
            events.append((name, data))

        result = await sandbox.exec("printf a1; printf b1 >&2; printf a2", on_output=on_output)
        assert result.exit_code == 0
        assert events, "no streaming callbacks fired"
        stdout = b"".join(data for name, data in events if name == "stdout")
        stderr = b"".join(data for name, data in events if name == "stderr")
        assert stdout == b"a1a2"
        assert stderr == b"b1"
        assert result.stdout == stdout
        assert result.stderr == stderr
    finally:
        await sandbox.destroy()


async def test_output_truncation_keeps_head_and_tail(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        result = await sandbox.exec("head -c 65536 /dev/zero | tr '\\000' x", max_output_bytes=1024)
        assert result.exit_code == 0
        assert result.truncated
        assert len(result.stdout) <= 1024
        assert result.stdout.startswith(b"x" * 512)
        assert result.stdout.endswith(b"x" * 512)
    finally:
        await sandbox.destroy()


async def test_cwd_and_env_take_effect(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        mkdir = await sandbox.exec(["mkdir", "-p", "subdir"])
        assert mkdir.exit_code == 0, mkdir.stderr
        written = await sandbox.exec(
            ["sh", "-c", "printf marker > marker.txt"], cwd="/workspace/subdir"
        )
        assert written.exit_code == 0, written.stderr
        read = await sandbox.exec(["cat", "subdir/marker.txt"])
        assert read.stdout == b"marker"
        env_result = await sandbox.exec('printf %s "$SPLEEF_X"', env={"SPLEEF_X": "v1"})
        assert env_result.stdout == b"v1"
        # spec env merges into every exec
        sandbox.spec = sandbox.spec.__class__(
            **{**sandbox.spec.__dict__, "env": {"SPLEEF_SPEC": "s1"}}
        )
        merged = await sandbox.exec('printf %s "$SPLEEF_SPEC"')
        assert merged.stdout == b"s1"
    finally:
        await sandbox.destroy()


async def test_env_sanitization_drops_operator_environment(novita_provider, monkeypatch) -> None:
    monkeypatch.setenv("SPLEEF_OPERATOR_MARKER", "should-not-leak")
    sandbox = await novita_provider.create(make_spec())
    try:
        names = await sandbox.exec("env | cut -d= -f1 | sort")
        assert names.exit_code == 0
        name_list = names.stdout.decode().splitlines()
        assert "SPLEEF_OPERATOR_MARKER" not in name_list
        assert "PATH" in name_list
        home = await sandbox.exec('printf %s "$HOME"')
        assert home.stdout == b"/home/agent"
    finally:
        await sandbox.destroy()


async def test_timeout_kills_process_tree(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        pid_file = "bg.pid"
        command = f"sleep 30 & echo $! > {pid_file}; wait"
        result = await sandbox.exec(command, timeout_s=0.5)
        assert result.timed_out
        assert result.exit_code == TIMEOUT_EXIT_CODE
        pid = (await sandbox.exec(["cat", pid_file])).stdout.decode().strip()
        assert pid.isdigit()
        # The background child must die with its process group. Bounded wait:
        # the loop exits on its own as soon as the child is gone.
        wait = await sandbox.exec(
            f'pid="{pid}"; n=0; while kill -0 "$pid" 2>/dev/null; do n=$((n+1)); '
            "[ $n -lt 200 ] || exit 9; sleep 0.05; done; echo gone",
            timeout_s=15,
        )
        assert wait.exit_code == 0, f"background child {pid} survived: {wait.stderr!r}"
        assert wait.stdout.strip() == b"gone"
    finally:
        await sandbox.destroy()


async def test_concurrent_exec(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:

        async def one(index: int) -> bytes:
            result = await sandbox.exec(f"printf 'task-{index}'")
            assert result.exit_code == 0
            return result.stdout

        outputs = await asyncio.gather(*(one(i) for i in range(4)))
        assert outputs == [f"task-{i}".encode() for i in range(4)]
    finally:
        await sandbox.destroy()


async def test_stdin(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        result = await sandbox.exec("head -c 4", stdin=b"abcdef")
        assert result.exit_code == 0
        assert result.stdout == b"abcd"
    finally:
        await sandbox.destroy()


async def test_commands_run_in_the_workspace_by_default(novita_provider) -> None:
    """The vendor default is the user's home; relative paths must still mean the workspace."""
    sandbox = await novita_provider.create(make_spec("cwd-default"))
    try:
        assert (await sandbox.exec(["mkdir", "-p", "sub"])).exit_code == 0
        written = await sandbox.exec(["sh", "-c", "printf here > f.txt"], cwd="/workspace/sub")
        assert written.exit_code == 0
        assert (await sandbox.exec(["cat", "sub/f.txt"])).stdout == b"here"
        assert (await sandbox.exec(["cat", "f.txt"], cwd="sub")).stdout == b"here"
    finally:
        await sandbox.destroy()


async def test_a_reclaimed_sandbox_timeout_means_sandbox_lost(novita_provider, monkeypatch) -> None:
    """A live Novita account answers TimeoutException for a sandbox that is gone."""
    from novita_sandbox import TimeoutException

    sandbox = await novita_provider.create(make_spec("reclaimed"))

    def gone(*args: object, **kwargs: object) -> None:
        raise TimeoutException("The sandbox was not found")

    monkeypatch.setattr(sandbox._handle.commands, "run", gone)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    monkeypatch.undo()
    await sandbox.destroy()


async def test_exec_on_destroyed_sandbox_raises(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec())
    await sandbox.destroy()
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])


async def test_lost_sandbox_raises_sandbox_lost(novita_provider, novita_cluster) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        novita_cluster.lose(sandbox.id)
        with pytest.raises(SandboxLost):
            await sandbox.exec(["true"])
    finally:
        await sandbox.destroy()
