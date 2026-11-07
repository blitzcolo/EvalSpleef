"""Fixtures: an EndpointManager wired to real stores, mock providers and the HTTP stand-in."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from evalspleef.control.config.providers import ModelAccountConfig, ProvidersFileConfig
from evalspleef.control.endpoints import EndpointManager, EndpointManagerConfig, IdleSpinAlert
from evalspleef.control.events import JsonlEventStore
from evalspleef.control.probes import ProbeConfig, ProbeRunner
from evalspleef.control.providers import MODEL_PROVIDER_GROUP, ProviderRegistry
from evalspleef.control.registry import ModelRegistry
from evalspleef.control.store import (
    EndpointRepository,
    EndpointTicketRepository,
    ModelSpecRepository,
    ProbeReportRepository,
    connect,
    migrate,
)
from evalspleef.core.events import EventEnvelope
from evalspleef.spi.gateway import PriceSpec
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelRole,
    ModelSpec,
    ValidationReport,
    VendorSource,
)
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_endpoint import (
    MockApiProvider,
    MockAssistedProvider,
    MockExternalProvider,
)
from evalspleef.testing.secrets import DictSecretResolver

API_BASE = "https://models.example.test/v1"
"""What the operator pastes back; the stand-in maps it to the local fake upstream."""
MODEL_ID = "qwen-32b"
SHA = "a" * 40
START = datetime(2026, 11, 7, tzinfo=UTC).timestamp()
EVENT_BATCH = "endpoints"
HF_TOKEN_REF = "vault://hf/eval"

_PROVIDERS: dict[str, type[Any]] = {
    "assisted": MockAssistedProvider,
    "api": MockApiProvider,
    "external": MockExternalProvider,
}


def _load_standin() -> Any:
    name = "evalspleef_control_tests_gateway_standin"
    if name not in sys.modules:
        path = Path(__file__).parent.parent / "probes" / "gateway_standin.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class ObservableClock(FakeClock):
    """FakeClock that announces when a coroutine starts sleeping, so tests can await it."""

    def __init__(self, start: float = START) -> None:
        super().__init__(start)
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await super().sleep(seconds)


class FakeHF(HFAccessChecker):
    """HFAccessChecker double with a scriptable preflight verdict."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.preflight_failures: tuple[tuple[str, bool, str], ...] = ()

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        self.calls.append("resolve")
        return SHA

    async def preflight(self, source: HFSource) -> ValidationReport:
        self.calls.append("preflight")
        return ValidationReport(ok=not self.preflight_failures, checks=self.preflight_failures)

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        self.calls.append("hashes")
        return {"config.json": "c0ffee"}

    async def ensure_mirror(self, source: HFSource) -> str:
        self.calls.append("mirror")
        return f"our-org/mirror-qwen-{source.revision[:7]}"

    async def verify_mirror(self, source: HFSource) -> bool:
        return True


@dataclass
class World:
    manager: EndpointManager
    models: ModelRegistry
    endpoints: EndpointRepository
    tickets: EndpointTicketRepository
    reports: ProbeReportRepository
    events: JsonlEventStore
    admin: InMemoryGatewayAdmin
    standin: Any
    server: FakeOpenAIServer
    clock: ObservableClock
    hf: FakeHF
    idle_alerts: list[IdleSpinAlert] = field(default_factory=list)

    async def events_in(self, batch_id: str = EVENT_BATCH) -> list[EventEnvelope]:
        return await self.events.read(batch_id)

    async def transitions(self, batch_id: str = EVENT_BATCH) -> list[tuple[str, str | None, str]]:
        """(event type, from_state, state) of every event, in order."""
        return [
            (str(e.type), e.payload["from_state"], e.payload["state"])
            for e in await self.events_in(batch_id)
        ]

    async def assert_row_matches_log(self, endpoint_id: str, batch_id: str = EVENT_BATCH) -> None:
        """The endpoint row is a projection: it must equal the state of the latest event."""
        row = await self.endpoints.get(endpoint_id)
        latest = [
            e for e in await self.events_in(batch_id) if e.payload["endpoint_id"] == endpoint_id
        ]
        assert row is not None
        assert latest[-1].payload["state"] == row.state


@pytest.fixture
def spec_factory() -> Callable[..., ModelSpec]:
    def make(
        *,
        slug: str = "qwen",
        version: int = 1,
        roles: frozenset[ModelRole] = frozenset({"target"}),
        hf: bool = False,
        params: Mapping[str, Any] | None = None,
    ) -> ModelSpec:
        source: HFSource | VendorSource = (
            HFSource(repo="org/qwen", revision="main", access="gated", token_ref=HF_TOKEN_REF)
            if hf
            else VendorSource(vendor="acme", model_id=MODEL_ID)
        )
        return ModelSpec(
            slug=slug,
            spec_version=version,
            family="qwen",
            roles=roles,
            source=source,
            deployment=DeploymentSpec(
                adapter="mock",
                account="acct",
                params={
                    "max_concurrency": 16,
                    "min_replicas": 1,
                    "max_replicas": 3,
                    **(params or {}),
                },
            ),
            declared_context_tokens=4096,
            declared_max_output_tokens=512,
            pricing_ref="ref:x@2026-11",
            license="apache-2.0",
            owner="ops",
        )

    return make


@pytest_asyncio.fixture
async def make_world(tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[World]]]:
    closers: list[Callable[[], Awaitable[None]]] = []
    with ExitStack() as exit_stack:

        async def build(
            *,
            mode: str = "assisted",
            provider_params: Mapping[str, Any] | None = None,
            probe_config: ProbeConfig | None = None,
            config: EndpointManagerConfig | None = None,
            token_expiry: Mapping[str, date | None] | None = None,
            **server_kwargs: Any,
        ) -> World:
            clock = ObservableClock()
            server = exit_stack.enter_context(FakeOpenAIServer(model=MODEL_ID, **server_kwargs))
            admin = InMemoryGatewayAdmin(clock=clock)
            standin = exit_stack.enter_context(
                _load_standin().GatewayStandIn(admin, {API_BASE: server.base_url})
            )

            db = await connect("sqlite:///:memory:")
            await migrate(db)
            events = JsonlEventStore(tmp_path / f"events-{len(closers)}", fsync=False)
            closers.extend([db.close, events.close])

            params: dict[str, Any] = {
                "cold_start_s": 0,
                "model": MODEL_ID,
                **(provider_params or {}),
            }
            account = ModelAccountConfig(
                adapter="mock",
                base_url=None if mode == "assisted" else API_BASE,
                params=params,
            )

            class _EntryPoint:
                name = "mock"

                def load(self) -> Any:
                    return partial(_PROVIDERS[mode], clock=clock)

            providers = ProviderRegistry(
                ProvidersFileConfig(model_accounts={"acct": account}),
                DictSecretResolver({HF_TOKEN_REF: "hf-token"}),
                entry_point_source=lambda group: (
                    [_EntryPoint()] if group == MODEL_PROVIDER_GROUP else []
                ),
            )
            models = ModelRegistry(ModelSpecRepository(db), now=lambda: "2026-11-07T00:00:00Z")
            endpoints = EndpointRepository(db)
            tickets = EndpointTicketRepository(db)
            reports = ProbeReportRepository(db)
            hf = FakeHF()
            idle_alerts: list[IdleSpinAlert] = []

            async def on_idle(alert: IdleSpinAlert) -> None:
                idle_alerts.append(alert)

            runner = ProbeRunner(
                admin,
                gateway_url=standin.url,
                clock=clock,
                config=probe_config
                or ProbeConfig(
                    request_timeout_s=30.0, long_request_timeout_s=60.0, throughput_requests=2
                ),
            )
            manager = EndpointManager(
                models=models,
                providers=providers,
                endpoints=endpoints,
                tickets=tickets,
                reports=reports,
                events=events,
                gateway=admin,
                probes=runner,
                price_for=lambda spec: PriceSpec(
                    input_per_mtok=1.0, output_per_mtok=2.0, basis="assumed", table_version="t1"
                ),
                hf=hf,
                clock=clock,
                config=config or EndpointManagerConfig(warm_ping_timeout_s=30.0),
                hf_token_expiry=token_expiry,
                idle_alert=on_idle,
            )
            return World(
                manager,
                models,
                endpoints,
                tickets,
                reports,
                events,
                admin,
                standin,
                server,
                clock,
                hf,
                idle_alerts,
            )

        yield build
        for close in closers:
            await close()
