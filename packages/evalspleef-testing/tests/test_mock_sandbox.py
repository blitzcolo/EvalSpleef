from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    TransferError,
)
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.spi.secrets import SecretRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities

DIGEST = "sha256:" + "0" * 64
LABELS = {"batch": "b1", "trial": "t1", "lease": "l1", "provider": "mock-sandbox"}


class FakeSecrets:
    def resolve(self, ref: SecretRef | str) -> str:
        return f"secret:{ref}"


def make_spec(**overrides: Any) -> SandboxSpec:
    values: dict[str, Any] = {
        "trial_id": "t1",
        "lease_id": "l1",
        "role": "agent",
        "os": OSFamily.LINUX,
        "arch": Arch.X86_64,
        "template": TemplateRef(kind="oci", ref="mock:latest", digest=DIGEST),
        "resources": Resources(cpus=1, memory_mb=512, disk_mb=1024),
        "network": NetworkPolicy(mode=NetMode.OFF),
        "labels": LABELS,
        "max_lifetime_s": 3600,
    }
    values.update(overrides)
    return SandboxSpec(**values)


def make_provider(clock: FakeClock | None = None, **kwargs: Any) -> MockSandboxProvider:
    return MockSandboxProvider(
        "mock-1",
        {},
        FakeSecrets(),
        clock=clock if clock is not None else FakeClock(start=1000.0),
        **kwargs,
    )


async def make_sandbox(provider: MockSandboxProvider, **overrides: Any) -> Sandbox:
    return await provider.create(make_spec(**overrides))


async def assert_process_gone(pid: int, bounded_s: float = 5.0) -> None:
    async with asyncio.timeout(bounded_s):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.02)


def test_default_capabilities_match_the_contract() -> None:
    caps = default_capabilities()
    assert caps.os_families == frozenset({OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.X86_64, Arch.ARM64})
    assert caps.net_modes == frozenset(
        {NetMode.OFF, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR, NetMode.FULL}
    )
    assert caps.allowlist_kinds == frozenset({"hostname"})
    assert caps.streaming and caps.stdin
    assert caps.snapshot == "none"
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.max_concurrency == 8
    assert caps.max_lifetime_s == 7200


async def test_exec_runs_shell_and_argv_and_merges_environment() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider, env={"BASE": "base"})
    try:
        result = await sandbox.exec('printf "%s-%s" "$BASE" "$CALL"', env={"CALL": "call"})
        assert result.exit_code == 0
        assert not result.timed_out
        assert result.stdout == b"base-call"
        assert result.stderr == b""

        literal = await sandbox.exec(["/bin/echo", "a;b"])
        assert literal.stdout == b"a;b\n"

        failure = await sandbox.exec("echo out; echo err >&2; exit 3")
        assert failure.exit_code == 3
        assert failure.stdout == b"out\n"
        assert failure.stderr == b"err\n"

        piped = await sandbox.exec(["cat"], stdin=b"hello stdin")
        assert piped.stdout == b"hello stdin"
    finally:
        await sandbox.destroy()


async def test_exec_cwd_points_inside_the_sandbox_workspace() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    try:
        (sandbox.root / "workspace" / "sub").mkdir()
        result = await sandbox.exec("pwd", cwd="/workspace/sub")
        assert result.stdout.decode().strip() == str((sandbox.root / "workspace" / "sub").resolve())
    finally:
        await sandbox.destroy()


async def test_exec_streams_output_in_order() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    chunks: list[tuple[str, bytes]] = []
    try:
        result = await sandbox.exec(
            "printf one; printf two >&2; printf three",
            on_output=lambda stream, data: chunks.append((stream, data)),
        )
        assert result.stdout == b"onethree"
        assert result.stderr == b"two"
        assert b"".join(data for stream, data in chunks if stream == "stdout") == b"onethree"
        assert b"".join(data for stream, data in chunks if stream == "stderr") == b"two"
    finally:
        await sandbox.destroy()


async def test_timeout_kills_the_whole_process_group() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    try:
        result = await sandbox.exec("sleep 30 & echo $!; wait", timeout_s=0.5)
        assert result.timed_out
        assert result.exit_code == TIMEOUT_EXIT_CODE
        child_pid = int(result.stdout.split()[0])
        await assert_process_gone(child_pid)
    finally:
        await sandbox.destroy()


async def test_output_beyond_the_limit_is_truncated_keeping_head_and_tail() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    try:
        result = await sandbox.exec(
            "printf 'a'; head -c 2000 /dev/zero | tr '\\0' 'x'; printf 'z'",
            max_output_bytes=100,
        )
        assert result.truncated
        assert len(result.stdout) == 100
        assert result.stdout[:1] == b"a"
        assert result.stdout[-1:] == b"z"
        assert result.stdout == b"a" + b"x" * 49 + b"x" * 49 + b"z"

        small = await sandbox.exec("printf ok", max_output_bytes=100)
        assert not small.truncated
        assert small.stdout == b"ok"
    finally:
        await sandbox.destroy()


async def test_upload_and_download_preserve_permission_bits(tmp_path: Path) -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    source = tmp_path / "tree"
    source.mkdir()
    script = source / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o750)
    data = source / "data.bin"
    data.write_bytes(bytes(range(256)))
    data.chmod(0o640)
    try:
        await sandbox.upload(source, "/workspace/tree")
        result = await sandbox.exec(["./tree/run.sh"])
        assert result.stdout == b"hi\n"

        destination = tmp_path / "back"
        await sandbox.download("/workspace/tree", destination)
        assert stat.S_IMODE((destination / "run.sh").stat().st_mode) == 0o750
        assert stat.S_IMODE((destination / "data.bin").stat().st_mode) == 0o640
        assert (destination / "data.bin").read_bytes() == data.read_bytes()
    finally:
        await sandbox.destroy()


async def test_upload_refuses_a_symlink_pointing_outside(tmp_path: Path) -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    link = tmp_path / "escape"
    link.symlink_to("/etc/passwd")
    try:
        with pytest.raises(TransferError, match="escapes"):
            await sandbox.upload(link, "/workspace/escape")
        assert not (sandbox.root / "workspace" / "escape").exists()
    finally:
        await sandbox.destroy()


async def test_download_refuses_a_symlink_pointing_outside(tmp_path: Path) -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    try:
        await sandbox.exec("ln -s /etc/passwd escape")
        with pytest.raises(TransferError, match="escapes"):
            await sandbox.download("/workspace/escape", tmp_path / "escape")
        assert not (tmp_path / "escape").exists()

        (tmp_path / "inside").write_text("payload")
        await sandbox.exec("ln -s inside link")
        await sandbox.download("/workspace/link", tmp_path / "link")
        assert (tmp_path / "link").is_symlink()
    finally:
        await sandbox.destroy()


async def test_destroy_is_idempotent_and_later_use_raises_sandbox_lost() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    root = sandbox.root
    await sandbox.destroy()
    await sandbox.destroy()
    assert not root.exists()
    with pytest.raises(SandboxLost):
        await sandbox.exec("true")
    assert await provider.list({}) == []


async def test_expired_sandbox_raises_sandbox_lost_and_reap_destroys_it() -> None:
    clock = FakeClock(start=1000.0)
    provider = make_provider(clock)
    sandbox = await make_sandbox(provider, max_lifetime_s=100)
    root = sandbox.root
    clock.advance(99.0)
    assert (await sandbox.exec("true")).exit_code == 0
    clock.advance(1.0)
    with pytest.raises(SandboxLost, match="lifetime"):
        await sandbox.exec("true")
    assert len(await provider.list({})) == 1
    assert await provider.reap({"batch": "b1"}, 10_000) == 1
    assert not root.exists()
    assert await provider.list({"batch": "b1"}) == []


async def test_list_and_reap_select_by_labels(tmp_path: Path) -> None:
    clock = FakeClock(start=1000.0)
    provider = make_provider(clock)
    first = await make_sandbox(provider, trial_id="t1", lease_id="l1")
    second = await make_sandbox(
        provider,
        trial_id="t2",
        lease_id="l2",
        labels={"batch": "b2", "trial": "t2", "lease": "l2", "provider": "mock-sandbox"},
    )
    assert len(await provider.list({"batch": "b1"})) == 1
    listed = await provider.list({"batch": "b1"})
    assert listed[0]["id"] == first.id
    assert len(await provider.list({})) == 2

    clock.advance(50.0)
    assert await provider.reap({"batch": "b2"}, 0) == 1
    assert second.destroyed
    assert not first.destroyed
    assert await provider.list({"batch": "b2"}) == []
    assert len(await provider.list({})) == 1
    await first.destroy()


async def test_reap_destroys_a_lost_sandbox() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec("true")
    assert await provider.reap({"trial": "t1"}, 10_000) == 1
    assert await provider.list({}) == []


async def test_create_rejects_os_arch_and_gui_mismatches() -> None:
    provider = make_provider()
    with pytest.raises(CapabilityNotSupported, match="os"):
        await provider.create(make_spec(os=OSFamily.WINDOWS))
    with pytest.raises(CapabilityNotSupported, match="gui"):
        await provider.create(make_spec(gui=True))
    arch_limited = replace(default_capabilities(), arches=frozenset({Arch.X86_64}))
    limited = make_provider(capabilities=arch_limited)
    with pytest.raises(CapabilityNotSupported, match="arch"):
        await limited.create(make_spec(arch=Arch.ARM64))


async def test_create_rejects_a_network_policy_the_capabilities_cannot_honour() -> None:
    provider = make_provider()
    with pytest.raises(PolicyNotEnforceable, match="cidr"):
        await provider.create(
            make_spec(network=NetworkPolicy(mode=NetMode.OFF, allow_cidrs=("10.0.0.0/8",)))
        )
    offline_only = replace(default_capabilities(), net_modes=frozenset({NetMode.OFF}))
    limited = make_provider(capabilities=offline_only)
    with pytest.raises(PolicyNotEnforceable, match="network mode"):
        await limited.create(make_spec(network=NetworkPolicy(mode=NetMode.FULL)))


async def test_failure_injection_for_create_lose_and_quota() -> None:
    provider = make_provider()
    provider.fail_next_creates(1, SandboxCreateError("vendor 5xx"))
    with pytest.raises(SandboxCreateError, match="vendor 5xx"):
        await provider.create(make_spec())
    sandbox = await provider.create(make_spec())

    provider.set_quota(1)
    with pytest.raises(QuotaExceeded):
        await provider.create(make_spec())
    await sandbox.destroy()
    second = await provider.create(make_spec())

    provider.lose(second.id)
    with pytest.raises(SandboxLost, match="lost"):
        await second.exec("true")
    await second.destroy()


def test_isolation_profile_covers_every_invariant() -> None:
    profile = make_provider().isolation_profile()
    assert set(profile.invariants) == {f"I{index}" for index in range(1, 17)}
    assert profile.declared_isolation == default_capabilities().isolation
    assert not profile.invariants["I1"].waived
    for key, entry in profile.invariants.items():
        assert entry.how, key
        if key != "I1":
            assert entry.waived, key
            assert entry.reason == "test double", key


async def test_preflight_reports_live_sandboxes() -> None:
    provider = make_provider()
    sandbox = await make_sandbox(provider)
    report = await provider.preflight()
    assert report["healthy"] is True
    assert report["live_sandboxes"] == 1
    await sandbox.destroy()
    assert (await provider.preflight())["live_sandboxes"] == 0
