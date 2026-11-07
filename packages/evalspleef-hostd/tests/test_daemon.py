from __future__ import annotations

import asyncio
import ssl
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography import x509

from evalspleef.hostd.daemon import DaemonConfig, HostDaemon, build_daemon, load_adapter, main
from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider

if TYPE_CHECKING:
    from conftest import SpecMaker, Stacks

TIMEOUT_S = 30


class SignalClock(FakeClock):
    """A fake clock that announces when a sleeper has parked, so tests need no polling."""

    def __init__(self, start: float = 0.0) -> None:
        super().__init__(start)
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await super().sleep(seconds)


async def test_ttl_guard_reclaims_while_disconnected_and_reports_after_reconnect(
    stacks: Stacks, spec: SpecMaker
) -> None:
    daemon_clock = FakeClock()
    stack, mock = await stacks.start(daemon_clock=daemon_clock)
    stack.daemon.control_timeout_s = 0.5
    remote = stack.remote("mock")
    sandbox = await remote.create(spec(max_lifetime_s=100))
    (backing,) = [s for s in mock.created_sandboxes() if s.id == sandbox.id]
    registration_port, gateway_port = stack.gateway.registration_port, stack.gateway.gateway_port

    await stack.gateway.stop()  # the control plane is unreachable from here on
    assert await stack.daemon.heartbeat_once() is False
    assert stack.daemon.connected is False

    daemon_clock.advance(101)
    reclaimed = await stack.daemon.ttl_guard_once()
    assert [(r.sandbox_id, r.reason) for r in reclaimed] == [(sandbox.id, "ttl")]
    assert backing.destroyed
    assert stack.daemon.inventory().sandbox_ids == []
    assert [r.sandbox_id for r in stack.daemon.pending_reclaimed] == [sandbox.id]

    stack.daemon.control_timeout_s = 20
    await stack.gateway.start(
        "127.0.0.1", registration_port=registration_port, gateway_port=gateway_port
    )
    assert await stack.daemon.heartbeat_once() is True
    assert stack.daemon.pending_reclaimed == []
    record = stack.registry.get("host-1")
    assert [(r.sandbox_id, r.trial_id) for r in record.reclaimed] == [(sandbox.id, "t-1")]


async def test_heartbeat_loop_beats_every_interval_on_the_injected_clock(
    stacks: Stacks,
) -> None:
    clock = SignalClock()
    registry_clock = FakeClock()
    stack, _ = await stacks.start(daemon_clock=clock, registry_clock=registry_clock)
    beats = asyncio.Queue[float]()
    original = stack.registry.heartbeat

    def observed(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = original(*args, **kwargs)
        beats.put_nowait(record.last_heartbeat)
        return record

    stack.registry.heartbeat = observed  # type: ignore[method-assign]
    loop_task = asyncio.create_task(stack.daemon.heartbeat_loop())
    try:
        assert await asyncio.wait_for(beats.get(), TIMEOUT_S) == 0.0
        await asyncio.wait_for(clock.sleeping.wait(), TIMEOUT_S)
        registry_clock.advance(15)
        clock.advance(15)
        assert await asyncio.wait_for(beats.get(), TIMEOUT_S) == 15.0
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_certificate_rotation_swaps_credentials_and_keeps_serving(
    stacks: Stacks, spec: SpecMaker
) -> None:
    clock = FakeClock(start=time.time())
    stack, _ = await stacks.start(daemon_clock=clock)
    daemon = stack.daemon
    before = daemon._credentials
    assert before is not None
    assert await daemon.rotate_if_due() is False
    clock.advance(24 * 86400)  # inside the seven day margin of a thirty day certificate
    assert await daemon.rotate_if_due() is True
    after = daemon._credentials
    assert after is not None
    assert after.cert_pem != before.cert_pem
    assert await daemon.heartbeat_once() is True
    served = await asyncio.to_thread(ssl.get_server_certificate, ("127.0.0.1", stack.service_port))
    assert x509.load_pem_x509_certificate(served.encode()).serial_number == (
        x509.load_pem_x509_certificate(after.cert_pem).serial_number
    )
    fresh = stack.remote("mock")
    await (await fresh.create(spec())).destroy()


def test_adapters_are_discovered_by_entry_point_name() -> None:
    assert load_adapter("mock-sandbox") is MockSandboxProvider
    with pytest.raises(ValueError, match="no sandbox adapter"):
        load_adapter("no-such-adapter")


async def test_unknown_provider_is_a_capability_error(stacks: Stacks, spec: SpecMaker) -> None:
    stack, _ = await stacks.start()
    other = stack.remote("not-hosted")
    with pytest.raises(CapabilityNotSupported):
        await other.create(spec())


def test_config_builds_a_daemon_with_entry_point_providers(tmp_path: Path) -> None:
    (tmp_path / "ca.pem").write_bytes(b"ca")
    config_file = tmp_path / "hostd.yaml"
    config_file.write_text(
        """
host_id: mini-01
listen: 127.0.0.1:7445
control:
  registration_address: 127.0.0.1:7443
  gateway_address: 127.0.0.1:7444
  ca_file: ca.pem
heartbeat_interval_s: 15
providers:
  - name: macos
    adapter: mock-sandbox
    max_concurrency: 2
"""
    )
    config = DaemonConfig.load(config_file)
    daemon = build_daemon(config)
    assert isinstance(daemon, HostDaemon)
    hosted = daemon.hosted("macos")
    assert isinstance(hosted.provider, MockSandboxProvider)
    assert hosted.capabilities().max_concurrency == 2


def test_main_requires_a_config_path() -> None:
    assert main([]) == 2


async def test_sandbox_types_are_the_mock_backend(stacks: Stacks, spec: SpecMaker) -> None:
    stack, mock = await stacks.start()
    sandbox = await stack.remote("mock").create(spec())
    description = await sandbox.describe()
    assert description["host"] == "mock"
    assert any(isinstance(s, MockSandbox) and s.id == sandbox.id for s in mock.created_sandboxes())
    await sandbox.destroy()
