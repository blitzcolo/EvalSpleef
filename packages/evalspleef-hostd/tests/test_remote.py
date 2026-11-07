"""RemoteProvider against a real loopback daemon (mTLS gRPC) in front of the mock sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from evalspleef.hostd import transfer
from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    TransferError,
)
from evalspleef.spi.sandbox import NetworkPolicy

if TYPE_CHECKING:
    from conftest import SpecMaker, Stacks

TIMEOUT_S = 30


async def test_exec_streams_output_in_order_and_returns_the_result(
    stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _ = await stacks.start()
    remote = stack.remote("mock")
    sandbox = await remote.create(spec())
    seen: list[tuple[str, bytes]] = []
    result = await sandbox.exec(
        "printf one; printf two >&2; printf three",
        on_output=lambda stream, data: seen.append((stream, data)),
    )
    assert result.exit_code == 0
    assert result.stdout == b"onethree"
    assert result.stderr == b"two"
    assert b"".join(d for s, d in seen if s == "stdout") == b"onethree"
    assert b"".join(d for s, d in seen if s == "stderr") == b"two"
    await sandbox.destroy()


async def test_exec_options_cross_the_wire(stacks: Stacks, spec: SpecMaker) -> None:
    stack, _ = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    stdin = await sandbox.exec(["cat"], stdin=b"piped")
    assert stdin.stdout == b"piped"
    env = await sandbox.exec('printf %s "$GREETING"', env={"GREETING": "hi"})
    assert env.stdout == b"hi"
    timed = await sandbox.exec("sleep 30", timeout_s=0.2)
    assert timed.timed_out and timed.exit_code == 124
    capped = await sandbox.exec("head -c 4096 /dev/zero", max_output_bytes=1024)
    assert capped.truncated and len(capped.stdout) <= 1024
    await sandbox.destroy()


async def test_capabilities_and_profile_round_trip(stacks: Stacks) -> None:
    stack, mock = await stacks.start()
    remote = stack.remote("mock")
    report = await remote.preflight()
    assert report["healthy"] is True
    assert remote.capabilities() == mock.capabilities()
    assert remote.isolation_profile() == mock.isolation_profile()


async def test_provider_errors_keep_their_type(stacks: Stacks, spec: SpecMaker) -> None:
    stack, mock = await stacks.start()
    remote = stack.remote("mock")
    with pytest.raises(CapabilityNotSupported):
        await remote.create(spec(gui=True))
    with pytest.raises(PolicyNotEnforceable):
        await remote.create(
            spec(network=NetworkPolicy(mode=NetMode.OFF, allow_cidrs=("10.0.0.0/8",)))
        )
    mock.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await remote.create(spec())
    mock.set_quota(0)
    with pytest.raises(QuotaExceeded):
        await remote.create(spec())
    mock.set_quota(5)
    sandbox = await remote.create(spec())
    with pytest.raises(CapabilityNotSupported):
        await sandbox.snapshot("s1")
    await sandbox.destroy()


async def test_unknown_sandbox_is_reported_lost(stacks: Stacks, spec: SpecMaker) -> None:
    stack, mock = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    mock.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()
    await sandbox.destroy()  # idempotent


async def test_listing_and_reaping_by_labels(stacks: Stacks, spec: SpecMaker) -> None:
    stack, _ = await stacks.start()
    remote = stack.remote("mock")
    first = await remote.create(spec("a"))
    await remote.create(spec("b"))
    listed = await remote.list({"trial": "a"})
    assert [entry["id"] for entry in listed] == [first.id]
    assert await remote.reap({"suite": "hostd"}, older_than_s=0) == 2
    assert await remote.list({"suite": "hostd"}) == []
    assert stack.daemon.inventory().sandbox_ids == []


async def test_multi_chunk_file_round_trip(stacks: Stacks, spec: SpecMaker, tmp_path: Path) -> None:
    stack, _ = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    source = tmp_path / "big.bin"
    source.write_bytes(os.urandom(9 << 20))  # three 4 MiB-bounded chunks plus tar framing
    await sandbox.upload(source, "/workspace/big.bin")
    back = tmp_path / "back.bin"
    await sandbox.download("/workspace/big.bin", back)
    assert (
        hashlib.sha256(back.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    )
    await sandbox.destroy()


async def test_download_corrupted_in_flight_is_detected(
    stacks: Stacks, spec: SpecMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, _ = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    source = tmp_path / "f.bin"
    source.write_bytes(b"payload" * 1000)
    await sandbox.upload(source, "/workspace/f.bin")

    class FlippingReader(transfer.ChunkReader):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for chunk in super().__iter__():
                chunk.data = bytes([chunk.data[0] ^ 0xFF]) + chunk.data[1:]
                yield chunk

    monkeypatch.setattr(transfer, "ChunkReader", FlippingReader)
    destination = tmp_path / "out.bin"
    with pytest.raises(TransferError, match="sha256"):
        await sandbox.download("/workspace/f.bin", destination)
    assert not destination.exists()
    await sandbox.destroy()


async def test_upload_corrupted_in_flight_is_refused_by_the_daemon(
    stacks: Stacks, spec: SpecMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, _ = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    source = tmp_path / "f.bin"
    source.write_bytes(b"payload" * 1000)

    class FlippingReader(transfer.ChunkReader):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for chunk in super().__iter__():
                chunk.data = bytes([chunk.data[0] ^ 0xFF]) + chunk.data[1:]
                yield chunk

    monkeypatch.setattr(transfer, "ChunkReader", FlippingReader)
    with pytest.raises(TransferError, match="sha256"):
        await sandbox.upload(source, "/workspace/f.bin")
    monkeypatch.undo()
    check = await sandbox.exec(["test", "-e", "/workspace/f.bin"])
    assert check.exit_code != 0, "a corrupted upload must not leave a file behind"
    await sandbox.destroy()


async def test_killed_server_mid_exec_surfaces_sandbox_lost(
    stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _ = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    started = asyncio.Event()
    task = asyncio.create_task(
        sandbox.exec("echo started; sleep 60", on_output=lambda *_: started.set())
    )
    await asyncio.wait_for(started.wait(), TIMEOUT_S)
    await stack.daemon.stop_serving(grace=None)
    with pytest.raises(SandboxLost):
        await asyncio.wait_for(task, TIMEOUT_S)
    with pytest.raises(SandboxLost):
        await asyncio.wait_for(sandbox.exec(["true"]), TIMEOUT_S)


async def test_drain_refuses_new_creates_and_keeps_existing_sandboxes(
    stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _ = await stacks.start()
    remote = stack.remote("mock")
    running = await remote.create(spec("running"))
    assert await remote.drain(True) == "draining"
    with pytest.raises(SandboxCreateError, match="draining"):
        await remote.create(spec("new"))
    still = await running.exec(["echo", "alive"])
    assert still.stdout.strip() == b"alive"
    inventory = await remote.inventory()
    assert inventory.mode == "draining"
    assert await stack.daemon.heartbeat_once()
    assert stack.registry.state("host-1") == "draining"
    assert await remote.drain(False) == "active"
    again = await remote.create(spec("again"))
    await again.destroy()
    await running.destroy()


async def test_maintenance_refuses_every_call(stacks: Stacks, spec: SpecMaker) -> None:
    stack, _ = await stacks.start()
    remote = stack.remote("mock")
    running = await remote.create(spec())
    assert await remote.set_maintenance(True) == "maintenance"
    with pytest.raises(SandboxCreateError):
        await remote.create(spec("new"))
    with pytest.raises(SandboxLost):
        await running.exec(["true"])
    with pytest.raises(SandboxCreateError):
        await remote.list({})
    assert await remote.set_maintenance(False) == "active"
    assert (await running.exec(["echo", "back"])).stdout.strip() == b"back"
    await running.destroy()


async def test_local_concurrency_cap_is_enforced_by_the_daemon(
    stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _ = await stacks.start(max_concurrency={"mock": 2})
    remote = stack.remote("mock")
    # The daemon shares this loop, so the blocking first fetch must come from the cache.
    await remote.preflight()
    assert remote.capabilities().max_concurrency == 2
    first = await remote.create(spec("1"))
    await remote.create(spec("2"))
    with pytest.raises(QuotaExceeded):
        await remote.create(spec("3"))
    await first.destroy()
    await (await remote.create(spec("4"))).destroy()


async def test_concurrent_creates_never_exceed_the_cap(stacks: Stacks, spec: SpecMaker) -> None:
    stack, _ = await stacks.start(max_concurrency={"mock": 2})
    remote = stack.remote("mock")
    results = await asyncio.gather(
        *(remote.create(spec(f"c{index}")) for index in range(5)), return_exceptions=True
    )
    created = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(created) == 2
    assert all(isinstance(item, QuotaExceeded) for item in failures)


async def test_lost_host_fails_calls_as_environment_errors(stacks: Stacks, spec: SpecMaker) -> None:
    from evalspleef.testing.clock import FakeClock

    registry_clock = FakeClock()
    stack, _ = await stacks.start(registry_clock=registry_clock)
    remote = stack.remote("mock", use_registry=True)
    sandbox = await remote.create(spec())
    assert await stack.daemon.heartbeat_once()
    registry_clock.advance(61)
    assert stack.registry.sweep() == ["host-1"]
    with pytest.raises(SandboxLost, match="lost"):
        await sandbox.exec(["true"])
    with pytest.raises(SandboxCreateError):
        await remote.create(spec("another"))
    assert await stack.daemon.heartbeat_once()
    assert stack.registry.state("host-1") == "active"
    assert (await sandbox.exec(["echo", "ok"])).stdout.strip() == b"ok"
