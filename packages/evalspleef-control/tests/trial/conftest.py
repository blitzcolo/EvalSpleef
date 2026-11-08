"""Harness for trial-flow tests: mock sandboxes, in-memory gateway, scripted runner/evaluator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from evalspleef.control.batch.executor import LeaseHandle, PlannedTrial, TrialLeases, TrialOutcome
from evalspleef.control.events import JsonlEventStore, project_trial_state
from evalspleef.control.taskpack import TaskPack, load_task_pack
from evalspleef.control.trial import (
    EvaluationResult,
    Evaluator,
    FeedbackWriter,
    ModelEndpoint,
    PackFeedbackWriter,
    TrialRunner,
    TrialSetup,
)
from evalspleef.control.trial.testing import ScriptedEvaluator, make_result
from evalspleef.core.canary import CanaryRegistry
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.feedback import ALLOWED_FIELDS, FeedbackDecision, LeakChecker, RegexLeakChecker
from evalspleef.core.models import FeedbackLevel, Severity, TrialState
from evalspleef.core.state import LEGAL_TRANSITIONS
from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.runner import TokenUsage
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxProvider,
    SandboxRole,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider
from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner
from evalspleef.testing.secrets import DictSecretResolver

REPO = Path(__file__).resolve().parents[4]
PACK_ROOT = REPO / "tasks" / "backend-001"
BATCH = "2026-11-b1"
TRIAL_ID = "backend-001.demo-model.scripted.e1.t01"
START = 1_788_000_000.0
TEMPLATE = TemplateRef("oci", "python:3.12-slim", "sha256:" + "a" * 64)


class ProbingProvider(MockSandboxProvider):
    """A mock provider whose isolation profile has executable probes for chosen invariants."""

    def __init__(self, instance: str, probes: Mapping[str, str], clock: FakeClock) -> None:
        super().__init__(instance, {}, DictSecretResolver(), clock=clock)
        self._probes = dict(probes)

    def isolation_profile(self) -> IsolationProfile:
        invariants = {
            f"I{number}": InvariantEntry(
                how="test double",
                probe=self._probes.get(f"I{number}"),
                waived=f"I{number}" not in self._probes,
                reason=None if f"I{number}" in self._probes else "test double",
            )
            for number in range(1, 17)
        }
        return IsolationProfile(
            provider=self.name,
            declared_isolation=self.capabilities().isolation,
            invariants=invariants,
        )


class Directory:
    def __init__(self, providers: Mapping[str, ProbingProvider]) -> None:
        self._providers = providers

    def sandbox_instance(self, name: str) -> SandboxProvider:
        return self._providers[name]


async def make_lease(
    provider: MockSandboxProvider,
    trial: PlannedTrial,
    role: SandboxRole,
    lease_id: str,
    env: Mapping[str, str] | None = None,
) -> LeaseHandle:
    requirements = trial.requirements
    spec = SandboxSpec(
        trial_id=trial.trial_id,
        lease_id=lease_id,
        role=role,
        os=requirements.os,
        arch=sorted(requirements.arch)[0],
        template=trial.template,
        resources=Resources(requirements.cpus, requirements.memory_mb, requirements.disk_mb),
        network=NetworkPolicy(mode=requirements.network if role == "agent" else NetMode.OFF),
        env=dict(env or {}),
        labels={"batch": trial.batch_id, "trial": trial.trial_id, "lease": lease_id},
        max_lifetime_s=1800,
    )
    return LeaseHandle(lease_id, role, provider.instance, None, await provider.create(spec))


class ScriptedReplacer:
    """Gives out the next agent sandbox on an instance that has not been tried yet."""

    def __init__(self, providers: Mapping[str, ProbingProvider]) -> None:
        self._providers = providers
        self.calls: list[frozenset[str]] = []

    async def replace(
        self, trial: PlannedTrial, failed: LeaseHandle, *, exclude: frozenset[str]
    ) -> LeaseHandle:
        self.calls.append(exclude)
        for instance, provider in self._providers.items():
            if instance not in exclude:
                return await make_lease(provider, trial, "agent", f"lease-agent-{len(self.calls)}")
        raise SandboxCreateError("no other instance left")


@dataclass
class Harness:
    runner: TrialRunner
    trial: PlannedTrial
    leases: TrialLeases
    events: JsonlEventStore
    gateway: InMemoryGatewayAdmin
    scripted: ScriptedRunner
    evaluator: Evaluator
    providers: dict[str, ProbingProvider]
    clock: FakeClock
    registry: CanaryRegistry
    pack: TaskPack
    replacer: ScriptedReplacer
    first_lease: LeaseHandle
    verifier_sandboxes: list[MockSandbox] = field(default_factory=list)

    async def run(self) -> TrialOutcome:
        return await self.runner.run_trial(self.trial, self.leases)

    async def log(self) -> list[EventEnvelope]:
        return await self.events.read(BATCH, trial_id=self.trial.trial_id)

    async def types(self) -> list[str]:
        return [event.type.value for event in await self.log()]

    async def of_type(self, *names: str) -> list[EventEnvelope]:
        return [event for event in await self.log() if event.type.value in names]

    def sandboxes(self) -> list[MockSandbox]:
        return [box for provider in self.providers.values() for box in provider.created_sandboxes()]


def _lease_prefix(trial_id: str) -> list[EventEnvelope]:
    """The events the driver and the lease manager append before the runner starts."""
    common = {"ts": "2026-11-07T00:00:00Z", "batch_id": BATCH, "trial_id": trial_id}
    lease = {"event": "lease", "from": "pending", "to": "leased"}
    return [
        EventEnvelope(
            event_id="ev_prefix_created",
            type=EventType.TRIAL_CREATED,
            actor="driver",
            payload={},
            **common,
        ),
        EventEnvelope(
            event_id="ev_prefix_leased",
            type=EventType.TRIAL_LEASED,
            actor="lease-manager",
            payload={"transition": lease},
            **common,
        ),
    ]


def assert_legal_transitions(events: Sequence[EventEnvelope]) -> list[tuple[str, str, str]]:
    """Every transition in the log is in the legal table and follows the previous one.

    The production projection (``project_trial_state``) must replay the same
    log, behind the lease events, to the state the last transition reached.
    """
    seen: list[tuple[str, str, str]] = []
    position = TrialState.LEASED
    for event in events:
        transition = event.payload.get("transition")
        if transition is None:
            continue
        source = TrialState(transition["from"])
        target = TrialState(transition["to"])
        assert source is position, f"{transition} does not continue from {position}"
        assert LEGAL_TRANSITIONS[source].get(transition["event"]) is target, transition
        position = target
        seen.append((transition["event"], source.value, target.value))
    if events:
        trial_id = events[0].trial_id or TRIAL_ID
        projected = project_trial_state([*_lease_prefix(trial_id), *events], trial_id=trial_id)
        assert projected is position, f"the event log projects to {projected}, not {position}"
    return seen


def default_pack() -> TaskPack:
    return load_task_pack(PACK_ROOT)


def severity_items(pack: TaskPack, severity: Severity) -> list[str]:
    return [i.id for i in pack.rubric.items if i.severity is severity]


@pytest.fixture
def pack() -> TaskPack:
    return default_pack()


@pytest.fixture
def blocking(pack: TaskPack) -> str:
    """A functional rubric item whose F1 message passes the default leak check."""
    writer = PackFeedbackWriter.load(pack)
    checker = RegexLeakChecker()
    for item_id in severity_items(pack, Severity.FUNCTIONAL):
        decision = FeedbackDecision(
            level=FeedbackLevel.F1,
            defect_id=f"{item_id}#1",
            allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F1],
        )
        text = writer.compose(decision)
        if text and not checker.check(text, FeedbackLevel.F1):
            return item_id
    raise AssertionError("the pack has no functional item with a clean F1 message")


@pytest.fixture
def minors(pack: TaskPack) -> list[str]:
    return severity_items(pack, Severity.MINOR)


@pytest.fixture
def legal() -> Callable[[Sequence[EventEnvelope]], list[tuple[str, str, str]]]:
    return assert_legal_transitions


class ObservingClock(FakeClock):
    """A fake clock that lets a test wait until a timer has been armed, without polling time."""

    def __init__(self, start: float) -> None:
        super().__init__(start)
        self._changed = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        inner = asyncio.ensure_future(super().sleep(seconds))
        await asyncio.sleep(0)  # one loop turn lets the inner sleep register its timer
        self._changed.set()
        await inner

    async def sleepers(self, count: int) -> None:
        """Return once at least ``count`` timers are armed."""
        while self.pending_sleepers < count:
            self._changed.clear()
            await self._changed.wait()


@pytest.fixture
def clock() -> ObservingClock:
    return ObservingClock(START)


@pytest.fixture
def rnd() -> Callable[..., ScriptedRound]:
    """A scripted agent round that writes a file and reports some usage."""

    def make(**overrides: Any) -> ScriptedRound:
        fields: dict[str, Any] = {
            "files": {"solution.txt": "work\n"},
            "final_message": "done",
            "usage": TokenUsage(1000, 500),
            "cost_usd": 0.25,
            "duration_s": 30.0,
        }
        return ScriptedRound(**{**fields, **overrides})

    return make


@pytest.fixture
async def harness_factory(
    tmp_path: Path, clock: ObservingClock
) -> AsyncIterator[Callable[..., Any]]:
    built: list[Harness] = []

    async def build(
        *,
        observations: Sequence[EvaluationResult] | None = None,
        final: EvaluationResult | None = None,
        rounds: Sequence[ScriptedRound] = (),
        budget: Mapping[str, Any] | None = None,
        instances: Sequence[str] = ("alpha",),
        probes: Mapping[str, Mapping[str, str]] | None = None,
        scripted: ScriptedRunner | None = None,
        evaluator: Evaluator | None = None,
        owns_terminal_events: bool = False,
        with_replacer: bool = True,
        agent_env: Mapping[str, str] | None = None,
        leak_checker: LeakChecker | None = None,
        writer_factory: Callable[[TaskPack], FeedbackWriter] | None = None,
        registry: CanaryRegistry | None = None,
    ) -> Harness:
        task_pack = default_pack()
        if budget:
            task_pack = replace(task_pack, budget=replace(task_pack.budget, **budget))
        providers = {
            name: ProbingProvider(name, (probes or {}).get(name, {}), clock) for name in instances
        }
        trial = PlannedTrial(
            trial_id=TRIAL_ID,
            batch_id=BATCH,
            cell_id="backend-001.demo-model.scripted",
            task_id="backend-001",
            model="demo-model",
            shell="scripted",
            epoch=1,
            index=1,
            requirements=task_pack.requirements,
            template=TEMPLATE,
            agent_routes=tuple(instances),
            verifier_routes=tuple(instances),
            endpoint_id="ep_demo_00000000",
        )
        first = providers[instances[0]]
        agent = await make_lease(first, trial, "agent", "lease-agent-0", agent_env)
        verifiers: list[MockSandbox] = []

        async def open_verifier() -> LeaseHandle:
            handle = await make_lease(first, trial, "verifier", "lease-verifier-1")
            assert isinstance(handle.sandbox, MockSandbox)
            verifiers.append(handle.sandbox)
            return handle

        leases = TrialLeases(agent, open_verifier)
        gateway = InMemoryGatewayAdmin(clock=clock)
        events = JsonlEventStore(tmp_path / f"events-{len(built)}", fsync=False)
        script = scripted or ScriptedRunner(rounds)
        judge = evaluator or ScriptedEvaluator(observations or [make_result()], final)
        shared = registry or CanaryRegistry()
        replacer = ScriptedReplacer(providers)

        def resolve(planned: PlannedTrial) -> TrialSetup:
            endpoint = ModelEndpoint("ep_demo_00000000", "ep_demo_00000000", "http://gw.test/v1")
            return TrialSetup(task_pack, endpoint)

        kwargs: dict[str, Any] = {}
        if writer_factory is not None:
            kwargs["writer_factory"] = writer_factory
        runner = TrialRunner(
            events=events,
            gateway=gateway,
            providers=Directory(providers),
            resolve=resolve,
            runner_factory=lambda planned: script,
            evaluator=judge,
            canary=shared,
            clock=clock,
            replacer=replacer if with_replacer else None,
            leak_checker=leak_checker,
            owns_terminal_events=owns_terminal_events,
            scratch_root=tmp_path,
            **kwargs,
        )
        harness = Harness(
            runner, trial, leases, events, gateway, script, judge, providers, clock, shared,
            task_pack, replacer, agent, verifiers,
        )  # fmt: skip
        built.append(harness)
        return harness

    yield build
    for harness in built:
        for sandbox in [*harness.sandboxes(), *harness.verifier_sandboxes]:
            await sandbox.destroy()
        await harness.events.close()
