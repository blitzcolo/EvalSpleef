"""Harness for batch tests: mock sandbox providers, in-memory store, a fake trial executor."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio

from evalspleef.control.batch import (
    BatchDriver,
    BatchPlan,
    BatchPlanner,
    EndpointStates,
    FreezeRecord,
    ModelPlanInput,
    PlannedTrial,
    TaskPlanInput,
    TrialLeases,
    TrialOutcome,
    build_freeze_record,
)
from evalspleef.control.events import EventStore, open_event_store
from evalspleef.control.leases import LeaseManager
from evalspleef.control.ledger import CostLedger
from evalspleef.control.scheduling import CapacitySpec, Scheduler, StaticMatcher
from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.control.store.repos import (
    GradeRepository,
    SandboxLeaseRepository,
    TrialRepository,
)
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily, RuntimeRequirements
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import SandboxCapabilities, SandboxProvider, TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver

BATCH = "2026-11-b1"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
TODAY = date(2026, 11, 7)
DIGEST = "sha256:" + "a" * 64


@dataclass(frozen=True)
class Pref:
    instance: str


class Directory:
    """Provider directory and sandbox provider lookup over mock providers."""

    def __init__(self, providers: Mapping[str, MockSandboxProvider]) -> None:
        self.providers = dict(providers)

    def route(self, *, os: OSFamily | str | None = None, role: str | None = None) -> list[Pref]:
        del role
        return [Pref(name) for name in self.providers] if str(os) == "linux" else []

    def capabilities(self, instance: str) -> SandboxCapabilities:
        return self.providers[instance].capabilities()

    def egress_proxy(self, instance: str) -> object | None:
        return None

    def sandbox_instance(self, name: str) -> SandboxProvider:
        return self.providers[name]

    def sandbox_instances(self) -> Mapping[str, SandboxProvider]:
        return self.providers


def scored_record(
    trial_id: str, tier: str = "A", penalty: int = 3, cost: float = 1.5
) -> dict[str, Any]:
    """A trial record as ``TrialRecord.to_dict`` shapes it, with one final grade."""
    return {
        "trial_id": trial_id,
        "state": "scored",
        "validity": "valid",
        "extensions": {
            "flow": {
                "usage": {
                    "total_tokens": 4000,
                    "agent_cost_usd": cost,
                    "judge_cost_usd": 0.25,
                    "user_cost_usd": 0.05,
                }
            }
        },
        "grades": [
            {
                "grade_version": 1,
                "tier": tier,
                "penalty": penalty,
                "status": "final",
                "decided_by": "auto",
                "counted_in_leaderboard": True,
                "cost": {"agent": cost},
                "flags": [],
                "decision": {"rule_id": "G-FIXTURE", "rationale": "fixture"},
            }
        ],
    }


#: The transitions a real trial runner records for a first-round pass, from ``leased``.
SCORED_PATH = (
    (EventType.TRIAL_STARTED, "start", "leased", "running"),
    (EventType.AGENT_STOPPED, "stop", "running", "observing"),
    (EventType.VERIFY_STARTED, "finish", "observing", "verifying"),
    (EventType.GRADE_COMPUTED, "score", "verifying", "scored"),
)


def fake_versions(trial: PlannedTrial) -> dict[str, str]:
    """The trial.versions a fake executor records on its start event."""
    return {"task_version": "1.0.0", "model_version": f"{trial.model}@1", "shell_version": "t@1"}


class FakeExecutor:
    """A ``TrialExecutor`` that opens the verifier, then scores, raises or blocks on a gate.

    Like the real runner it records each transition it makes as an event, so
    the driver's projection of the trial row has the same input as in production.
    """

    def __init__(self, events: EventStore) -> None:
        self.events = events
        self.running = 0
        self.peak = 0
        self.calls: list[str] = []
        self.raises: dict[str, BaseException] = {}
        self.gate: asyncio.Event | None = None
        self.arrived: asyncio.Queue[str] = asyncio.Queue()
        self.leases_seen: dict[str, tuple[str, str]] = {}
        """Trial id -> (agent lease id, verifier lease id)."""
        self.extra_versions: dict[str, Any] = {}
        """Recorded on every start event next to :func:`fake_versions`."""

    async def run_trial(self, trial: PlannedTrial, leases: TrialLeases) -> TrialOutcome:
        self.calls.append(trial.trial_id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.arrived.put_nowait(trial.trial_id)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                for _ in range(3):
                    await asyncio.sleep(0)  # yield so other trials can run concurrently
            if trial.trial_id in self.raises:
                raise self.raises[trial.trial_id]
            verifier = await leases.open_verifier()
            self.leases_seen[trial.trial_id] = (leases.agent.lease_id, verifier.lease_id)
            for event_type, event, before, after in SCORED_PATH:
                await self.events.append(
                    EventEnvelope(
                        event_id=f"ev_{trial.trial_id}_{event}",
                        ts="2026-11-07T12:00:00Z",
                        batch_id=trial.batch_id,
                        type=event_type,
                        actor="fake-executor",
                        trial_id=trial.trial_id,
                        payload={
                            "transition": {"event": event, "from": before, "to": after},
                            **(
                                {"versions": {**fake_versions(trial), **self.extra_versions}}
                                if event == "start"
                                else {}
                            ),
                        },
                    )
                )
            return TrialOutcome("scored", record=scored_record(trial.trial_id))
        finally:
            self.running -= 1


class AlwaysWarm:
    async def state(self, endpoint_id: str) -> Any:
        raise AssertionError(f"hosted endpoint {endpoint_id} must not be queried")


@dataclass
class Harness:
    clock: FakeClock
    directory: Directory
    db: Database
    events: EventStore
    trials: TrialRepository
    grades: GradeRepository
    lease_rows: SandboxLeaseRepository
    scheduler: Scheduler
    manager: LeaseManager
    executor: FakeExecutor
    planner: BatchPlanner
    models: list[ModelPlanInput]
    tasks: list[TaskPlanInput]
    epochs: int
    budget_cap: float | None
    profiles: dict[str, IsolationProfile]
    freeze_kwargs: dict[str, Any]
    plan: BatchPlan
    freeze: FreezeRecord
    driver: BatchDriver
    providers: dict[str, MockSandboxProvider] = field(default_factory=dict)
    sleeps: list[float] = field(default_factory=list)
    """Backoff waits the driver asked for; the harness records them instead of waiting."""

    def refreeze(
        self,
        *,
        models: Sequence[ModelPlanInput] | None = None,
        tasks: Sequence[TaskPlanInput] | None = None,
        profiles: Mapping[str, IsolationProfile] | None = None,
        **freeze_kwargs: Any,
    ) -> FreezeRecord:
        """Plan and freeze again with some inputs changed."""
        plan = self.planner.plan(
            BATCH,
            models or self.models,
            tasks or self.tasks,
            epochs=self.epochs,
            budget_cap_usd=self.budget_cap,
        )
        kwargs = {**self.freeze_kwargs, **freeze_kwargs}
        return build_freeze_record(plan, isolation_profiles=profiles or self.profiles, **kwargs)

    async def events_of(self, *types: EventType) -> list[EventEnvelope]:
        return [e for e in await self.events.read(BATCH) if e.type in types]

    def freeze_and_run(self) -> Awaitable[Any]:
        self.driver.freeze(lint_passed=True, budget_approved=True)
        return self.driver.run()


def make_task(task_id: str = "web-001", **requirement_changes: Any) -> TaskPlanInput:
    requirements = RuntimeRequirements(
        os=OSFamily.LINUX,
        arch=frozenset({Arch.X86_64}),
        min_isolation=Isolation.L1,
        network=NetMode.OFF,
        cpus=1,
        memory_mb=1024,
        max_lifetime_s=1800,
    )
    return TaskPlanInput(
        task_id=task_id,
        version="1.0.0",
        requirements=replace(requirements, **requirement_changes),
        template=TemplateRef("oci", f"registry.test/{task_id}:1", DIGEST),
        max_cost_usd=15.0,
    )


def make_model(slug: str = "alpha", **changes: Any) -> ModelPlanInput:
    base = ModelPlanInput(
        slug=slug,
        spec_version=1,
        shell="terminus",
        endpoint_id=f"ep_{slug}_00000000",
        probe_report_id=f"pr_{slug}",
    )
    return replace(base, **changes)


@pytest.fixture
def task_factory() -> Callable[..., TaskPlanInput]:
    return make_task


@pytest.fixture
def model_factory() -> Callable[..., ModelPlanInput]:
    return make_model


@pytest_asyncio.fixture
async def build() -> AsyncIterator[Callable[..., Awaitable[Harness]]]:
    closers: list[Callable[[], Awaitable[None]]] = []

    async def make(
        *,
        instances: Sequence[str] = ("docker-a",),
        capacity: Sequence[CapacitySpec] | None = None,
        provider_quota: int | None = None,
        provider_caps: SandboxCapabilities | None = None,
        n_models: int = 1,
        n_tasks: int = 1,
        models: Sequence[ModelPlanInput] | None = None,
        tasks: Sequence[TaskPlanInput] | None = None,
        epochs: int = 1,
        budget_cap: float | None = None,
        profiles: Mapping[str, IsolationProfile] | None = None,
        endpoint_states: EndpointStates | None = None,
        deadline_s: float | None = None,
        **freeze_overrides: Any,
    ) -> Harness:
        clock = FakeClock()
        providers = {
            name: MockSandboxProvider(
                name,
                {},
                DictSecretResolver(),
                clock=clock,
                capabilities=provider_caps or default_capabilities(),
            )
            for name in instances
        }
        if provider_quota is not None:
            for provider in providers.values():
                provider.set_quota(provider_quota)
        directory = Directory(providers)
        db = await connect("sqlite:///:memory:")
        await migrate(db)
        events = await open_event_store("sqlite:///:memory:")
        closers.extend([db.close, events.close])

        model_list = list(
            models or [make_model(slug) for slug in ["alpha", "beta", "gamma"][:n_models]]
        )
        task_list = list(tasks or [make_task(f"web-{n:03d}") for n in range(1, n_tasks + 1)])
        planner = BatchPlanner(StaticMatcher(directory))
        plan = planner.plan(BATCH, model_list, task_list, epochs=epochs, budget_cap_usd=budget_cap)
        profile_map = dict(profiles or {n: p.isolation_profile() for n, p in providers.items()})
        freeze_kwargs: dict[str, Any] = {
            "gateway_version": "gw-1.0",
            "price_table_version": "prices-2026-11",
            "runner_versions": {"terminus": "0.3.0"},
            "template_digests": {
                task.template.ref: {name: DIGEST for name in instances} for task in task_list
            },
            "frozen_at": "2026-11-07T12:00:00Z",
        }
        freeze_kwargs.update(freeze_overrides)
        freeze = build_freeze_record(plan, isolation_profiles=profile_map, **freeze_kwargs)

        scheduler = Scheduler(
            capacity or [CapacitySpec(name, configured_limit=4) for name in instances],
            clock=clock.now,
        )
        counter = itertools.count(1)
        lease_rows = SandboxLeaseRepository(db)
        manager = LeaseManager(
            directory, lease_rows, events, now=lambda: NOW, new_id=lambda: f"{next(counter):04d}"
        )
        executor = FakeExecutor(events)
        trials = TrialRepository(db)
        grades = GradeRepository(db)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        driver = BatchDriver(
            plan=plan,
            freeze=freeze,
            executor=executor,
            scheduler=scheduler,
            leases=manager,
            trials=trials,
            grades=grades,
            events=events,
            endpoint_states=endpoint_states or AlwaysWarm(),
            isolation_profiles=profile_map,
            now=lambda: NOW,
            deadline_s=deadline_s,
            today=TODAY,
            ledger=CostLedger(db),
            sleep=record_sleep,
        )
        return Harness(
            clock=clock,
            directory=directory,
            db=db,
            events=events,
            trials=trials,
            grades=grades,
            lease_rows=lease_rows,
            scheduler=scheduler,
            manager=manager,
            executor=executor,
            planner=planner,
            models=model_list,
            tasks=task_list,
            epochs=epochs,
            budget_cap=budget_cap,
            profiles=profile_map,
            freeze_kwargs=freeze_kwargs,
            plan=plan,
            freeze=freeze,
            driver=driver,
            providers=providers,
            sleeps=sleeps,
        )

    yield make
    for close in reversed(closers):
        await close()
