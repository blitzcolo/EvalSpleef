"""Fixtures: a lease manager over a mock sandbox provider, in-memory store and event log."""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from evalspleef.control.events import EventStore, open_event_store
from evalspleef.control.leases import LeaseManager, LeaseRequest
from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.control.store.repos import SandboxLeaseRepository
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily, RuntimeRequirements
from evalspleef.spi.sandbox import SandboxProvider, TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

BATCH = "2026-11-b1"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
TEMPLATE = TemplateRef(kind="oci", ref="registry.test/web:1", digest="sha256:" + "a" * 64)


class Providers:
    """``SandboxProviders`` over a fixed set of instances."""

    def __init__(self, instances: Mapping[str, SandboxProvider]) -> None:
        self._instances = dict(instances)

    def sandbox_instance(self, name: str) -> SandboxProvider:
        return self._instances[name]

    def sandbox_instances(self) -> Mapping[str, SandboxProvider]:
        return self._instances


@dataclass
class LeaseEnv:
    clock: FakeClock
    provider: MockSandboxProvider
    db: Database
    events: EventStore
    leases: SandboxLeaseRepository
    manager: LeaseManager
    make_manager: Callable[..., LeaseManager]

    async def event_types(self, trial_id: str | None = None) -> list[str]:
        return [str(e.type) for e in await self.events.read(BATCH, trial_id=trial_id)]

    async def events_of(self, event_type: EventType) -> list[EventEnvelope]:
        return [e for e in await self.events.read(BATCH) if e.type == event_type]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[LeaseEnv]:
    clock = FakeClock()
    provider = MockSandboxProvider("docker-a", {}, DictSecretResolver(), clock=clock)
    db = await connect("sqlite:///:memory:")
    await migrate(db)
    events = await open_event_store("sqlite:///:memory:")
    leases = SandboxLeaseRepository(db)

    def make_manager(
        providers: Mapping[str, SandboxProvider] | None = None, **kwargs: Any
    ) -> LeaseManager:
        counter = itertools.count(1)
        return LeaseManager(
            Providers(providers or {"docker-a": provider}),
            leases,
            events,
            now=lambda: NOW,
            new_id=kwargs.pop("new_id", lambda: f"{next(counter):04d}"),
            **kwargs,
        )

    yield LeaseEnv(clock, provider, db, events, leases, make_manager(), make_manager)
    await events.close()
    await db.close()


@pytest.fixture
def request_for() -> Callable[..., LeaseRequest]:
    def make(trial_id: str = "web-001.alpha.terminus.e1.t01", **changes: Any) -> LeaseRequest:
        requirements = RuntimeRequirements(
            os=OSFamily.LINUX,
            arch=frozenset({Arch.X86_64}),
            min_isolation=Isolation.L1,
            network=NetMode.OFF,
            max_lifetime_s=1800,
        )
        base = LeaseRequest(
            batch_id=BATCH,
            trial_id=trial_id,
            role="agent",
            instance="docker-a",
            requirements=requirements,
            template=TEMPLATE,
            host_id="host-1",
        )
        return replace(base, **changes)

    return make
