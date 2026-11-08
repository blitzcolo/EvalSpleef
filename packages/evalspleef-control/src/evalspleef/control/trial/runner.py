"""The trial runner: one trial from lease to grade (PRD appendix B.2-B.4, TECH-DESIGN section 10).

``TrialRunner`` is the ``TrialExecutor`` the batch driver calls with an already leased agent
sandbox. It drives ``core.state.TrialMachine`` and appends an event for every state change: the
event that carries a transition has a ``transition`` entry in its payload.

Event ownership follows ``batch.executor``: the driver (through ``LeaseManager``) appends
``trial.scored``, ``trial.invalidated``, ``teardown.*`` and destroys the leased sandboxes. With
``owns_terminal_events=True`` the runner does all of that itself, for use without a driver.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal, TypeVar

from evalspleef.core.canary import CanaryRegistry, CanaryTrial, scan_trials
from evalspleef.core.defects import DefectCatalog, DefectTracker
from evalspleef.core.error_taxonomy import ERROR_TAXONOMY
from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.core.feedback import (
    FeedbackDecision,
    FeedbackLadder,
    FeedbackRecord,
    FeedbackRound,
    FeedbackValidation,
    LeakChecker,
    RegexLeakChecker,
    build_feedback_record,
    consume_decision,
    validate_feedback_message,
)
from evalspleef.core.grade_inputs import GradingInputs
from evalspleef.core.models import (
    Budget,
    BudgetUsage,
    CheckKind,
    DefectProgress,
    EscalationPolicy,
    FeedbackLevel,
    TrialState,
)
from evalspleef.core.state import (
    IllegalTransition,
    TerminalDecision,
    TrialMachine,
    evaluate_terminal,
    event_path_for_terminal,
    record_grade,
)
from evalspleef.core.versions import TrialVersions
from evalspleef.spi.errors import EndpointMismatch, SpleefError
from evalspleef.spi.gateway import ENDPOINT_MISMATCH, GatewayAdmin, TrialKey
from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage

from ..batch.executor import LeaseHandle, PlannedTrial, TrialExecutor, TrialLeases, TrialOutcome
from ..events import EventStore
from ..leases import LeaseInvariantError
from ..taskpack.models import Budget as PackBudget
from ..taskpack.models import TaskPack
from .errors import EvaluationTimeout, IsolationPreflightFailed, RunnerCrashed
from .feedback_text import PackFeedbackWriter
from .prepare import (
    StagedWorkspace,
    check_identity_hygiene,
    run_isolation_probes,
    stage_workspace,
    upload_and_verify,
)
from .protocols import (
    AgentSandboxReplacer,
    Clock,
    Evaluator,
    FeedbackWriter,
    ProviderDirectory,
    RunnerFactory,
    TrialSetupResolver,
)
from .types import (
    Artifact,
    CheatSignal,
    EvaluationResult,
    TrialContext,
    TrialSetup,
    WorkspacePaths,
)

__all__ = ["ErrorVerdict", "TrialRunner", "classify_error"]

ACTOR = "trial-runner"
MAX_PREFLIGHT_RETRIES = 2
"""Isolation preflight failures are retried on another instance twice (PRD B.2a, S5)."""
MAX_LEAK_REWRITES = 3
MAX_CLARIFICATIONS = 3
MAX_CRASH_RETRIES = 1
HEARTBEAT_INTERVAL_S = 15.0
MISSED_HEARTBEATS = 3
"""PRD B.3: the shell beats every 15 s; three missed beats in a row mean it hangs."""
KEY_EXPIRY_BUFFER_S = 900
THRESHOLD_RATIO = 0.8
MAX_SCAN_BYTES = 8 << 20

T = TypeVar("T")


@dataclass(frozen=True)
class ErrorVerdict:
    """How an exception ends a trial attempt."""

    status: Literal["failed_infra", "invalid"]
    error_class: str | None
    disposition: str | None


def classify_error(exc: BaseException) -> ErrorVerdict:
    """Map an exception through the SPI error taxonomy, the way the batch driver does.

    Retryable errors and environment losses are infrastructure failures (the trial is retried
    as a new record); everything else, including unknown exceptions, invalidates the attempt.
    """
    error_class: str | None = None
    disposition: str | None = None
    for klass in type(exc).__mro__:
        entry = ERROR_TAXONOMY.get(klass.__name__)
        if entry is not None:
            error_class, disposition = entry
            break
    else:
        if isinstance(exc, SpleefError):
            error_class, disposition = exc.error_class, exc.disposition
    retryable = isinstance(exc, SpleefError) and exc.retryable
    infra = (
        retryable
        or disposition == "retry"
        or (error_class == "environment" and disposition == "discard")
    )
    return ErrorVerdict("failed_infra" if infra else "invalid", error_class, disposition)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _core_budget(budget: PackBudget) -> Budget:
    return Budget(
        max_rounds=budget.max_rounds,
        max_feedback_rounds=budget.max_feedback_rounds,
        max_nudges=budget.max_nudges,
        max_total_tokens=budget.max_total_tokens,
        max_wall_clock_s=budget.max_wall_clock_s,
        max_cost_usd=budget.max_cost_usd,
        max_observe_s=budget.max_observe_s,
    )


def _workspace_paths(pack: TaskPack) -> WorkspacePaths:
    block = pack.metadata.get("workspace")
    if not isinstance(block, Mapping):
        return WorkspacePaths("/workspace", None, ())
    starter = block.get("starter")
    entries = block.get("entry_files") or ()
    return WorkspacePaths(
        remote_root=str(block.get("mount") or "/workspace"),
        starter_dir=pack.file(starter) if isinstance(starter, str) and starter else None,
        entry_files=tuple(str(entry) for entry in entries),
    )


def _read_text(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.read(MAX_SCAN_BYTES).decode("utf-8", errors="replace")


class _DeadlineExceeded(Exception):
    """An awaited step ran past its deadline on the injected clock."""


class _AgentHung(Exception):
    """The agent runner missed ``MISSED_HEARTBEATS`` heartbeats in a row."""


@dataclass(frozen=True)
class _Plan:
    terminal: TerminalDecision | None
    feedback: FeedbackDecision | None
    cannot_continue: bool


def verification_events(
    pack: TaskPack, final: EvaluationResult
) -> list[tuple[EventType, dict[str, Any]]]:
    """``hiddentest.result`` and ``judge.verdict`` for a final evaluation (PRD B.8).

    Script items are the hidden tests; ``llm_judge`` items are the judge panel's verdicts.
    A pack without judge items emits no ``judge.verdict``.
    """
    kinds = {item.id: item.kind for item in pack.rubric.items}

    def verdicts(kind: CheckKind) -> list[dict[str, str]]:
        return [
            {"item_id": result.item_id, "verdict": str(result.verdict)}
            for result in final.rubric
            if kinds.get(result.item_id) is kind
        ]

    hidden = verdicts(CheckKind.SCRIPT)
    events: list[tuple[EventType, dict[str, Any]]] = [
        (
            EventType.HIDDENTEST_RESULT,
            {
                "round": "final",
                "items": hidden,
                "passed": sum(1 for item in hidden if item["verdict"] == "pass"),
                "total": len(hidden),
            },
        )
    ]
    judged = verdicts(CheckKind.LLM_JUDGE)
    if judged:
        events.append(
            (
                EventType.JUDGE_VERDICT,
                {
                    "round": "final",
                    "items": judged,
                    "agreement": final.judge_consistency,
                    "samples": final.judge_samples,
                    "cost_usd": final.judge_cost_usd,
                },
            )
        )
    return events


class TrialRunner(TrialExecutor):
    """Runs trials end to end. Holds no per-trial state, so one instance serves a whole batch."""

    def __init__(
        self,
        *,
        events: EventStore,
        gateway: GatewayAdmin,
        providers: ProviderDirectory,
        resolve: TrialSetupResolver,
        runner_factory: RunnerFactory,
        evaluator: Evaluator,
        canary: CanaryRegistry,
        clock: Clock,
        replacer: AgentSandboxReplacer | None = None,
        writer_factory: Callable[[TaskPack], FeedbackWriter] = PackFeedbackWriter.load,
        leak_checker: LeakChecker | None = None,
        owns_terminal_events: bool = False,
        scratch_root: Path | None = None,
    ) -> None:
        self.events = events
        self.gateway = gateway
        self.providers = providers
        self.resolve = resolve
        self.runner_factory = runner_factory
        self.evaluator = evaluator
        self.canary = canary
        self.clock = clock
        self.replacer = replacer
        self.writer_factory = writer_factory
        self.leak_checker: LeakChecker = leak_checker or RegexLeakChecker()
        self.owns_terminal_events = owns_terminal_events
        self.scratch_root = scratch_root

    async def run_trial(self, trial: PlannedTrial, leases: TrialLeases) -> TrialOutcome:
        return await _TrialRun(self, trial, leases).execute()


class _TrialRun:
    """All state of one trial attempt."""

    setup: TrialSetup
    budget: Budget
    ladder: FeedbackLadder
    tracker: DefectTracker
    writer: FeedbackWriter
    ctx: TrialContext
    scratch: Path

    def __init__(self, owner: TrialRunner, trial: PlannedTrial, leases: TrialLeases) -> None:
        self.owner = owner
        self.trial = trial
        self.leases = leases
        self.agent: LeaseHandle = leases.agent
        self.machine = TrialMachine(trial.trial_id, state=TrialState.LEASED)
        self.usage = BudgetUsage()
        self.history: list[FeedbackRound] = []
        self.feedback_records: list[FeedbackRecord] = []
        self.cheat: list[CheatSignal] = []
        self.artifacts: list[Artifact] = []
        self.transcript: list[str] = []
        self.stop_reasons: list[str] = []
        self.usage_records_seen = 0
        self.heartbeats = False
        """Whether the agent runner sends liveness beats (``AgentRunner.watch_heartbeat``)."""
        self.last_beat = 0.0
        """Gateway usage records already read after earlier turns."""
        self.versions: dict[str, Any] | None = None
        self.preflight_log: list[dict[str, Any]] = []
        self.placements: list[dict[str, str]] = []
        self.announced: set[tuple[str, float]] = set()
        self.last_failing: set[str] = set()
        self.key: TrialKey | None = None
        self.agent_runner: AgentRunner | None = None
        self.scratch_created = False

    # -- entry point ---------------------------------------------------------------------

    async def execute(self) -> TrialOutcome:
        try:
            return await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # recorded as an incident event and mapped, never swallowed
            return await self._fail(exc)
        finally:
            await self._teardown()

    # -- the flow ------------------------------------------------------------------------

    async def _run(self) -> TrialOutcome:
        owner, trial = self.owner, self.trial
        self.setup = owner.resolve(trial)
        pack = self.setup.pack
        self.budget = _core_budget(pack.budget)
        self.ladder = FeedbackLadder(
            EscalationPolicy(
                f1_no_progress_rounds=pack.budget.escalation.f1_no_progress_rounds,
                f2_no_progress_rounds=pack.budget.escalation.f2_no_progress_rounds,
            )
        )
        # The pack's defect catalogue lists only feedbackable items; grading tracks every item.
        self.tracker = DefectTracker(DefectCatalog.from_rubric_set(pack.rubric))
        self.writer = owner.writer_factory(pack)
        workspace = _workspace_paths(pack)
        self.scratch = Path(tempfile.mkdtemp(prefix="evalspleef-trial-", dir=owner.scratch_root))
        self.scratch_created = True

        token = owner.canary.register(trial.trial_id)
        self.machine.record.canary_token = token
        staged = await asyncio.to_thread(
            stage_workspace,
            pack=pack,
            workspace=workspace,
            trial_id=trial.trial_id,
            token=token,
            scratch=self.scratch,
        )
        self.placements = [
            {"path": p.path, "kind": p.kind, "sha256": staged.hashes[p.path]}
            for p in staged.placements
        ]
        await self._emit(EventType.CANARY_INJECTED, {"placements": self.placements})

        self.agent = await self._prepare_agent(staged, workspace)
        self.ctx = TrialContext(
            trial=trial,
            pack=pack,
            endpoint=self.setup.endpoint,
            budget=self.budget,
            escalation=self.ladder.escalation,
            workspace=workspace,
            agent=self.agent,
            leases=self.leases,
            canary_token=token,
            instruction=staged.instruction,
            scratch_dir=self.scratch,
        )

        self.key = await owner.gateway.issue_key(
            trial_id=trial.trial_id,
            endpoint_id=self.setup.endpoint.endpoint_id,
            max_budget_usd=self.budget.max_cost_usd,
            max_tokens=self.budget.max_total_tokens,
            expires_at=_iso(owner.clock.now() + self.budget.max_wall_clock_s + KEY_EXPIRY_BUFFER_S),
        )
        self.agent_runner = owner.runner_factory(trial)
        self.heartbeats = self.agent_runner.watch_heartbeat(self._beat)
        self.versions = self._trial_versions(self.agent_runner).to_dict()
        await self._advance(
            "start",
            EventType.TRIAL_STARTED,
            {
                "lease_id": self.agent.lease_id,
                "provider_instance": self.agent.provider_instance,
                "endpoint_id": self.setup.endpoint.endpoint_id,
                "key_id": self.key.key_id,
                "key_expires_at": self.key.expires_at,
                "versions": self.versions,
            },
        )
        await self.agent_runner.start(
            self.agent.sandbox,
            gateway_url=self.setup.endpoint.gateway_url,
            trial_key=self.key.secret,
            model_alias=self.setup.endpoint.model_alias,
            instruction=staged.instruction,
            budget=RunnerBudget(
                max_total_tokens=self.budget.max_total_tokens,
                max_cost_usd=self.budget.max_cost_usd,
                max_wall_clock_s=self.budget.max_wall_clock_s,
            ),
        )
        return await self._rounds(staged.instruction)

    async def _prepare_agent(
        self, staged: StagedWorkspace, workspace: WorkspacePaths
    ) -> LeaseHandle:
        """S3-S5 on the leased sandbox; a failed preflight moves on to another instance."""
        owner = self.owner
        agent = self.leases.agent
        tried = {agent.provider_instance}
        attempt = 0
        while True:
            try:
                await self._prepare_once(agent, staged, workspace, attempt)
                return agent
            except IsolationPreflightFailed:
                if attempt >= MAX_PREFLIGHT_RETRIES or owner.replacer is None:
                    raise
                await agent.sandbox.destroy()
                agent = await owner.replacer.replace(self.trial, agent, exclude=frozenset(tried))
                self.agent = agent  # teardown must see the replacement even if it fails too
                tried.add(agent.provider_instance)
                attempt += 1

    async def _prepare_once(
        self, agent: LeaseHandle, staged: StagedWorkspace, workspace: WorkspacePaths, attempt: int
    ) -> None:
        trial = self.trial
        await upload_and_verify(agent.sandbox, staged, workspace.remote_root, self.scratch)
        await check_identity_hygiene(
            agent.sandbox,
            identifiers=[
                trial.trial_id,
                trial.task_id,
                trial.model,
                trial.batch_id,
                "evalspleef",
                "harbor",
            ],
            workspace_root=workspace.remote_root,
        )
        profile = self.owner.providers.sandbox_instance(agent.provider_instance).isolation_profile()
        results = await run_isolation_probes(agent.sandbox, profile)
        failed = [result for result in results if not result.passed]
        payload = {
            "attempt": attempt,
            "lease_id": agent.lease_id,
            "provider_instance": agent.provider_instance,
            "passed": not failed,
            "probes": [result.to_dict() for result in results],
            "waived": sorted(profile.waivers()),
        }
        self.preflight_log.append(payload)
        await self._emit(EventType.PREFLIGHT_RESULT, payload)
        if failed:
            raise IsolationPreflightFailed(
                f"isolation probes failed on {agent.provider_instance}: "
                + ", ".join(result.invariant for result in failed),
                detail={"invariants": [result.invariant for result in failed]},
            )

    # -- rounds ----------------------------------------------------------------------------

    async def _rounds(self, first_message: str) -> TrialOutcome:
        message, kind = first_message, "initial"
        clarifications = crash_retries = 0
        while True:
            await self._emit(
                EventType.ROUND_STARTED, {"round": self.usage.rounds + 1, "kind": kind}
            )
            self.transcript.append(f"user: {message}")
            turn, elapsed = await self._send(message)
            cold_s = await self._after_turn()
            if turn.stop_reason is StopReason.CRASH:
                self._account(turn, elapsed, cold_s, count_round=False)
                await self._step_event(turn, counted=False)
                if crash_retries >= MAX_CRASH_RETRIES:
                    raise RunnerCrashed("the agent runner crashed again after a retry")
                crash_retries += 1
                kind = "crash_retry"
                continue
            if turn.stop_reason is StopReason.CANCELLED:
                self._account(turn, elapsed, cold_s, count_round=False)
                await self._step_event(turn, counted=False)
                raise RunnerCrashed("the agent runner was cancelled")
            if turn.stop_reason is StopReason.NEED_CLARIFICATION and (
                clarifications < MAX_CLARIFICATIONS
            ):
                # A reasonable question is answered from the FAQ; it is no feedback and no round.
                self._account(turn, elapsed, cold_s, count_round=False)
                await self._step_event(turn, counted=False)
                clarifications += 1
                self._record_reply(turn)
                message, kind = self.writer.answer(turn.final_message or ""), "clarification"
                continue

            self._account(turn, elapsed, cold_s, count_round=True)
            self._mark_exhausted(turn.stop_reason)
            self._record_reply(turn)
            self.stop_reasons.append(turn.stop_reason.value)
            round_no = self.usage.rounds
            await self._step_event(turn, counted=True)
            await self._threshold_events()
            await self._advance(
                "stop",
                EventType.AGENT_STOPPED,
                {"round": round_no, "stop_reason": turn.stop_reason.value},
            )
            await self._emit(EventType.OBSERVE_STARTED, {"round": round_no})
            result = await self._evaluate(round_no, final=False)
            await self._observed(round_no, result)

            plan = self._plan(result, turn.stop_reason)
            defect_payload = {
                "round": round_no,
                "open": [d.defect_id for d in self.tracker.active_defects()],
                "blocking_open": [d.defect_id for d in self.tracker.blocking_open()],
                "summary": self.tracker.summary(),
            }
            if plan.terminal is not None:
                await self._emit(EventType.DEFECT_UPDATED, defect_payload)
                return await self._conclude(
                    plan.terminal.reason, plan.terminal, cannot_continue=plan.cannot_continue
                )
            assert plan.feedback is not None  # no terminal and no decision cannot happen
            await self._advance("needs_feedback", EventType.DEFECT_UPDATED, defect_payload)
            delivery, text = await self._deliver(plan.feedback)
            if delivery == "blocked":
                return await self._conclude("feedback_blocked", None, feedback_blocked=True)
            if delivery == "unavailable":
                await self._advance(
                    "finish", EventType.VERIFY_STARTED, {"reason": "feedback unavailable"}
                )
                return await self._verify(cannot_continue=True)
            await self._threshold_events()
            message, kind = text, plan.feedback.level.value

    async def _send(self, message: str) -> tuple[ShellTurn, float]:
        """One agent turn under the wall-clock budget; a missed deadline ends it as ``timeout``."""
        assert self.agent_runner is not None
        clock = self.owner.clock
        remaining = self.budget.max_wall_clock_s - self.usage.wall_clock_s
        started = clock.now()
        timed_out = remaining <= 0
        if not timed_out:
            work = self.agent_runner.send(message, deadline_s=max(1, int(remaining)))
            try:
                turn = await self._race(self._watched(work) if self.heartbeats else work, remaining)
            except _DeadlineExceeded:
                timed_out = True
            except _AgentHung:
                await self.agent_runner.stop()
                await self._emit(
                    EventType.INCIDENT_OPENED,
                    {
                        "kind": "agent_hung",
                        "round": self.usage.rounds + 1,
                        "missed_beats": MISSED_HEARTBEATS,
                    },
                )
                elapsed = clock.now() - started
                hung = ShellTurn(
                    stop_reason=StopReason.CRASH,
                    final_message=None,
                    steps=0,
                    usage=TokenUsage(),
                    cost_usd=0.0,
                    duration_s=elapsed,
                )
                return hung, elapsed
        if timed_out:
            await self.agent_runner.stop()
            turn = ShellTurn(
                stop_reason=StopReason.TIMEOUT,
                final_message=None,
                steps=0,
                usage=TokenUsage(),
                cost_usd=0.0,
                duration_s=max(remaining, 0.0),
            )
        return turn, clock.now() - started

    def _beat(self) -> None:
        self.last_beat = self.owner.clock.now()

    async def _watched(self, work: Awaitable[T]) -> T:
        """Await ``work``; raise ``_AgentHung`` once ``MISSED_HEARTBEATS`` beats in a row are missed."""
        clock = self.owner.clock
        self.last_beat = clock.now()

        async def watchdog() -> None:
            while clock.now() - self.last_beat < MISSED_HEARTBEATS * HEARTBEAT_INTERVAL_S:
                await clock.sleep(HEARTBEAT_INTERVAL_S)

        job = asyncio.ensure_future(work)
        dog = asyncio.ensure_future(watchdog())
        try:
            await asyncio.wait({job, dog}, return_when=asyncio.FIRST_COMPLETED)
            if job.done():
                return job.result()
            raise _AgentHung
        finally:
            job.cancel()
            dog.cancel()
            await asyncio.gather(job, dog, return_exceptions=True)

    async def _race(self, work: Awaitable[T], seconds: float) -> T:
        """Await ``work`` for at most ``seconds`` of the injected clock."""
        job = asyncio.ensure_future(work)
        timer = asyncio.ensure_future(self.owner.clock.sleep(seconds))
        try:
            await asyncio.wait({job, timer}, return_when=asyncio.FIRST_COMPLETED)
            if job.done():
                return job.result()
            raise _DeadlineExceeded
        finally:
            job.cancel()
            timer.cancel()
            await asyncio.gather(job, timer, return_exceptions=True)

    async def _after_turn(self) -> float:
        """Read the turn's gateway records; return the cold-start seconds the gateway absorbed.

        A refused answer for the provider lock voids the trial (FR-MOD-04): another vendor
        served the model, so whatever the agent did afterwards no longer measures it. Cold
        start waits are kept out of the trial wall clock but logged (FR-MOD-06, FR-FLOW-06).
        """
        records = await self.owner.gateway.usage(self.trial.trial_id)
        refused = [record for record in records if record.error_code == ENDPOINT_MISMATCH]
        if refused:
            raise EndpointMismatch(
                f"{len(refused)} upstream answer(s) came from a vendor the provider lock refuses",
                detail={"reason": "provider_lock", "rounds": sorted({r.round for r in refused})},
            )
        fresh = records[self.usage_records_seen :]
        self.usage_records_seen = len(records)
        cold_ms = sum(record.cold_start_ms for record in fresh)
        if cold_ms > 0:
            await self._emit(
                EventType.ENDPOINT_COLD_START,
                {
                    "round": self.usage.rounds + 1,
                    "endpoint_ids": sorted({r.endpoint_id for r in fresh if r.cold_start_ms}),
                    "cold_start_ms": cold_ms,
                },
            )
        return cold_ms / 1000

    def _account(
        self, turn: ShellTurn, elapsed: float, cold_s: float, *, count_round: bool
    ) -> None:
        tokens = turn.usage.input_tokens + turn.usage.output_tokens
        wall = max(max(elapsed, turn.duration_s) - cold_s, 0.0)
        if count_round:
            self.usage.add_round(tokens=tokens, agent_cost_usd=turn.cost_usd, wall_clock_s=wall)
        else:
            self.usage.total_tokens += tokens
            self.usage.agent_cost_usd += turn.cost_usd
            self.usage.wall_clock_s += wall

    def _mark_exhausted(self, stop_reason: StopReason) -> None:
        """A cap-type stop reason means that budget item is spent, whatever the counters say."""
        usage, budget = self.usage, self.budget
        if stop_reason is StopReason.TIMEOUT:
            usage.wall_clock_s = max(usage.wall_clock_s, float(budget.max_wall_clock_s))
        elif stop_reason is StopReason.COST_CAP:
            usage.agent_cost_usd = max(usage.agent_cost_usd, budget.max_cost_usd)
        elif stop_reason is StopReason.TOKEN_CAP:
            usage.total_tokens = max(usage.total_tokens, budget.max_total_tokens)

    def _record_reply(self, turn: ShellTurn) -> None:
        if turn.final_message:
            self.transcript.append(f"agent: {turn.final_message}")

    async def _step_event(self, turn: ShellTurn, *, counted: bool) -> None:
        await self._emit(
            EventType.AGENT_STEP,
            {
                "round": self.usage.rounds,
                "counted": counted,
                "stop_reason": turn.stop_reason.value,
                "steps": turn.steps,
                "input_tokens": turn.usage.input_tokens,
                "output_tokens": turn.usage.output_tokens,
                "cost_usd": turn.cost_usd,
                "duration_s": turn.duration_s,
                "trace_ref": turn.trace_ref,
            },
        )

    async def _threshold_events(self) -> None:
        usage, budget = self.usage, self.budget
        dimensions = {
            "rounds": (usage.rounds, budget.max_rounds),
            "feedback_rounds": (usage.feedback_rounds, budget.max_feedback_rounds),
            "tokens": (usage.total_tokens, budget.max_total_tokens),
            "wall_clock_s": (usage.wall_clock_s, budget.max_wall_clock_s),
            "cost_usd": (usage.agent_cost_usd, budget.max_cost_usd),
        }
        for name, (used, limit) in dimensions.items():
            if limit <= 0:
                continue
            for mark in (THRESHOLD_RATIO, 1.0):
                if used / limit >= mark and (name, mark) not in self.announced:
                    self.announced.add((name, mark))
                    await self._emit(
                        EventType.BUDGET_THRESHOLD,
                        {"dimension": name, "threshold": mark, "used": used, "limit": limit},
                    )

    # -- observation and decisions ------------------------------------------------------------

    async def _evaluate(self, round_no: int, *, final: bool) -> EvaluationResult:
        try:
            return await self._race(
                self.owner.evaluator.evaluate(self.ctx, round_no, final), self.budget.max_observe_s
            )
        except _DeadlineExceeded:
            raise EvaluationTimeout(
                f"{'verification' if final else 'observation'} exceeded "
                f"{self.budget.max_observe_s} s"
            ) from None

    async def _observed(self, round_no: int, result: EvaluationResult) -> None:
        """Fold one observation into the defect tracker and the event log."""
        self._track(round_no, result)
        await self._emit(
            EventType.HARDGATE_RESULT,
            {
                "round": round_no,
                "passed": result.hard_gates_passed,
                "gates": [gate.to_dict() for gate in result.hard_gates],
            },
        )
        if result.artifacts:
            self.artifacts = list(result.artifacts)
            await self._emit(
                EventType.EVIDENCE_COLLECTED,
                {
                    "round": round_no,
                    "artifacts": [
                        {"path": a.path, "sha256": a.sha256, "size_bytes": a.size_bytes}
                        for a in result.artifacts
                    ],
                },
            )
        await self._note_cheat(round_no, result.cheat_signals)

    async def _note_cheat(self, label: int | str, signals: tuple[CheatSignal, ...]) -> None:
        """Record cheat signals not seen before and open one incident for them."""
        fresh = [signal for signal in signals if signal not in self.cheat]
        if not fresh:
            return
        self.cheat.extend(fresh)
        await self._emit(
            EventType.INCIDENT_OPENED,
            {
                "kind": "cheat",
                "round": label,
                "signals": [
                    {"kind": s.kind, "detail": s.detail, "evidence_uri": s.evidence_uri}
                    for s in fresh
                ],
            },
        )

    def _track(self, round_no: int, result: EvaluationResult) -> None:
        active = {defect.rubric_item_id for defect in self.tracker.active_defects()}
        # An inconclusive item keeps an existing defect alive; it never opens a new one.
        failing = [
            *result.failing_item_ids,
            *(item for item in result.inconclusive_item_ids if item in active),
        ]
        evidence = {
            item.item_id: item.evidence
            for item in result.rubric
            if item.verdict == "fail" and item.evidence
        }
        self.tracker.observe(
            round_no, failing, evidence=evidence, partial_item_ids=result.partial_item_ids
        )
        self.last_failing = set(failing)
        if self.history:
            last = self.history[-1]
            if last.defect_ids and last.made_progress is None:
                defect = self.tracker.get(last.defect_ids[0])
                last.progress = defect.progress.value
                last.made_progress = defect.progress is not DefectProgress.NONE

    def _plan(self, result: EvaluationResult, stop_reason: StopReason) -> _Plan:
        """The terminal decision of PRD B.4 or, failing that, the next feedback message."""
        exhausted = bool(self.usage.exhausted_reasons(self.budget))
        decision = self.ladder.next_decision(self.tracker, self.history, self.budget, self.usage)
        if (
            decision is None
            and not exhausted
            and stop_reason in (StopReason.IDLE, StopReason.COMPLETED)
            and self.usage.nudges < self.budget.max_nudges
        ):
            # Nothing the user could point at: the generic "go on" nudge, counted as F0.
            decision = FeedbackDecision(
                level=FeedbackLevel.F0,
                defect_id=None,
                allowed_fields=frozenset(),
                reason="no defect to report; generic nudge",
            )
        cannot_continue = stop_reason is StopReason.LOOP_DETECTED or (
            decision is None and not exhausted
        )
        terminal = evaluate_terminal(
            cheat_detected=bool(self.cheat),
            hard_gates_passed=result.hard_gates_passed,
            has_unresolved_blocking_defect=bool(self.tracker.blocking_open()),
            budget_exhausted=exhausted,
            cannot_continue=cannot_continue,
        )
        return _Plan(terminal, decision, cannot_continue)

    async def _conclude(
        self,
        reason: str,
        terminal: TerminalDecision | None,
        *,
        cannot_continue: bool = False,
        feedback_blocked: bool = False,
    ) -> TrialOutcome:
        payload = {"reason": reason, "terminal": None if terminal is None else terminal.to_dict()}
        if self.machine.state is TrialState.OBSERVING:
            await self._advance("finish", EventType.VERIFY_STARTED, payload)
        else:
            await self._emit(EventType.VERIFY_STARTED, payload)
        return await self._verify(
            cannot_continue=cannot_continue, feedback_blocked=feedback_blocked
        )

    # -- feedback ----------------------------------------------------------------------------

    def _validate(self, decision: FeedbackDecision, text: str) -> FeedbackValidation:
        if not text.strip():
            return FeedbackValidation(ok=False, reason="empty message")
        ids = () if decision.defect_id is None else (decision.defect_id,)
        return validate_feedback_message(
            level=decision.level, defect_ids=ids, text=text, checker=self.owner.leak_checker
        )

    async def _deliver(
        self, decision: FeedbackDecision
    ) -> tuple[Literal["sent", "blocked", "unavailable"], str]:
        """Write, leak-check (rewriting up to three times) and hand over one feedback message."""
        owner = self.owner
        nudge = decision.level is FeedbackLevel.F0
        text = self.writer.nudge(self.usage.nudges) if nudge else self.writer.compose(decision)
        if not text.strip():
            return "unavailable", ""
        index = len(self.feedback_records) + 1
        await self._emit(
            EventType.FEEDBACK_DECIDED, {"feedback_index": index, "decision": decision.to_dict()}
        )
        rewrites = 0
        check = self._validate(decision, text)
        while not check.ok and rewrites < MAX_LEAK_REWRITES:
            rewrites += 1
            text = self.writer.rewrite(text, check.violations)
            check = self._validate(decision, text)
        record = build_feedback_record(
            trial_id=self.trial.trial_id,
            feedback_index=index,
            decision=decision,
            text=text,
            template_id=f"{decision.level.value}-{'PERSONA' if nudge else 'PACK-TEXT'}",
            decided_at=_iso(owner.clock.now()),
            budget=self.budget,
            usage=self.usage,
            checker=owner.leak_checker,
            rewrite_count=rewrites,
            blocked=not check.ok,
        )
        if check.ok:
            # core marks rewrite_count == 3 as blocked; a third rewrite that passes is deliverable.
            record.leak_check.update(final_action="send", passed=True)
        self.feedback_records.append(record)
        leak_payload = {"feedback_index": index, "leak_check": record.leak_check}
        if not check.ok:
            await self._advance("feedback_blocked", EventType.FEEDBACK_LEAKCHECK, leak_payload)
            return "blocked", text
        await self._emit(EventType.FEEDBACK_LEAKCHECK, leak_payload)
        if decision.defect_id is not None:
            self.tracker.mark_feedback(decision.defect_id, decision.level)
        consume_decision(self.usage, decision)
        self.history.append(
            FeedbackRound(
                round_no=len(self.history) + 1,
                level=decision.level,
                defect_ids=() if decision.defect_id is None else (decision.defect_id,),
                text=text,
            )
        )
        await self._advance("deliver", EventType.FEEDBACK_SENT, {"feedback": record.to_dict()})
        return "sent", text

    # -- verification and grade --------------------------------------------------------------

    async def _verify(
        self, *, cannot_continue: bool = False, feedback_blocked: bool = False
    ) -> TrialOutcome:
        final = await self._evaluate(self.usage.rounds, final=True)
        await self._note_cheat("final", final.cheat_signals)
        if final.artifacts:
            self.artifacts = list(final.artifacts)
        verifier = self.leases.verifier
        verifier_id = verifier.lease_id if verifier is not None else final.verifier_lease_id
        if verifier_id is not None and verifier_id == self.agent.lease_id:
            raise LeaseInvariantError("the verifier shares the agent's lease (I10)")
        await self._emit(
            EventType.HARDGATE_RESULT,
            {
                "round": "final",
                "passed": final.hard_gates_passed,
                "gates": [gate.to_dict() for gate in final.hard_gates],
                "verifier_lease_id": verifier_id,
            },
        )
        for event_type, payload in verification_events(self.setup.pack, final):
            await self._emit(event_type, payload)

        reasons = list(final.manual_reasons)
        if final.inconclusive_item_ids:
            reasons.append("inconclusive rubric items: " + ", ".join(final.inconclusive_item_ids))
        if not final.hard_gates:
            reasons.append("the evaluator reported no hard gates")
        if reasons:
            return await self._pause(reasons)

        if set(final.failing_item_ids) != self.last_failing:
            self.tracker.observe(
                self.tracker.last_round + 1,
                final.failing_item_ids,
                evidence={i.item_id: i.evidence for i in final.rubric if i.evidence},
                partial_item_ids=final.partial_item_ids,
            )
        self.usage.judge_cost_usd += final.judge_cost_usd
        pack = self.setup.pack
        inputs = GradingInputs(
            trial_id=self.trial.trial_id,
            defects=tuple(self.tracker.defects),
            hard_gates=tuple(final.hard_gates),
            feedback_rounds=tuple(self.history),
            budget=self.budget,
            usage=self.usage,
            cheat=bool(self.cheat),
            cheat_evidence="; ".join(f"{s.kind}: {s.detail}" for s in self.cheat),
            feedback_blocked=feedback_blocked,
            cannot_continue=cannot_continue,
            judge_consistency_value=final.judge_consistency,
            judge_samples=final.judge_samples,
            rubric_version=pack.rubric.rubric_version,
            task_version=pack.version,
            pricing_table_version=pack.budget.pricing_table_version,
        )
        outcome = inputs.grade(decided_at=_iso(self.owner.clock.now()))
        grade = outcome.grade
        if grade is None:
            return await self._pause(
                list(outcome.manual_reasons) or ["no grade produced"], inputs=inputs
            )
        await self._advance(
            "score",
            EventType.GRADE_COMPUTED,
            {
                "rule_id": outcome.rule_id,
                "tier": grade.tier.value,
                "penalty": grade.penalty,
                "flags": [flag.value for flag in grade.flags],
                "cost": grade.cost.to_dict(),
                "grade": grade.to_dict(),
                # Recorded so a regrade can re-run the pure grading on the same inputs.
                "inputs": inputs.to_dict(),
            },
        )
        record_grade(self.machine.record, grade)
        return await self._scan_canary()

    async def _scan_canary(self) -> TrialOutcome:
        """Look for any trial's token in this trial's artifacts and transcript (invariant I13)."""
        owner, trial = self.owner, self.trial
        artifacts: dict[str, str] = {}
        for artifact in self.artifacts:
            if artifact.local_path is not None and artifact.local_path.is_file():
                artifacts[artifact.path] = await asyncio.to_thread(_read_text, artifact.local_path)
        report = scan_trials(
            [
                CanaryTrial(
                    trial_id=trial.trial_id,
                    token=owner.canary.token_of(trial.trial_id),
                    artifacts=artifacts,
                    trace=list(self.transcript),
                )
            ],
            token_owner=owner.canary.by_token(),
        )
        scan = {
            "passed": report.passed,
            "scanned_trials": list(report.scanned_trials),
            "tokens_seen": report.tokens_seen,
            "violations": [
                {
                    "kind": v.kind,
                    "trial_id": v.trial_id,
                    "token_owner": v.token_owner,
                    "location": v.location,
                    "severity": v.severity,
                }
                for v in report.violations
            ],
        }
        if report.passed:
            await self._emit(EventType.CANARY_SCANNED, scan)
            if owner.owns_terminal_events:
                grade = self.machine.record.current_grade()
                assert grade is not None
                await self._emit(EventType.TRIAL_SCORED, {"tier": grade.tier.value, "grades": 1})
            return TrialOutcome("scored", record=self._record_dict())
        await self._advance("invalidate_post_hoc", EventType.CANARY_SCANNED, scan)
        await self._emit(EventType.INCIDENT_OPENED, {"kind": "canary_cross_trial", **scan})
        reason = "canary token of another trial found in this trial"
        await self._terminal_invalid(reason)
        return TrialOutcome("invalid", record=self._record_dict(), reason=reason)

    async def _pause(
        self, reasons: list[str], *, inputs: GradingInputs | None = None
    ) -> TrialOutcome:
        payload: dict[str, Any] = {
            "kind": "manual_review",
            "reasons": reasons,
            "cost": self.usage.cost.to_dict(),
        }
        if inputs is not None:
            payload["inputs"] = inputs.to_dict()
        await self._advance("pause", EventType.INCIDENT_OPENED, payload)
        return TrialOutcome("paused_manual", record=self._record_dict(), reason="; ".join(reasons))

    async def _terminal_invalid(self, reason: str) -> None:
        if self.owner.owns_terminal_events:
            await self._emit(
                EventType.TRIAL_INVALIDATED,
                {"outcome": "invalid", "reason": reason, "retry": False},
            )

    # -- failure and teardown ------------------------------------------------------------------

    async def _fail(self, exc: Exception) -> TrialOutcome:
        """Map an exception to ``failed_infra`` or ``invalid``; the event records the cause."""
        verdict = classify_error(exc)
        message = exc.message if isinstance(exc, SpleefError) else str(exc)
        reason = f"{type(exc).__name__}: {message}"
        payload = {
            "kind": "trial_error",
            "exception": type(exc).__name__,
            "message": message,
            "error_class": verdict.error_class,
            "disposition": verdict.disposition,
            "outcome": verdict.status,
        }
        # observing and awaiting_feedback have no failed_infra transition in the table: the
        # observation cannot continue, so it ends and the trial fails from verifying.
        if (
            verdict.status == "failed_infra"
            and not self.machine.can("fail_infra")
            and self.machine.can("finish")
        ):
            await self._advance(
                "finish", EventType.INCIDENT_OPENED, {**payload, "kind": "observation_aborted"}
            )
        if verdict.status == "failed_infra" and self.machine.can("fail_infra"):
            await self._advance("fail_infra", EventType.INCIDENT_OPENED, payload)
        else:
            await self._emit(EventType.INCIDENT_OPENED, payload)
            if verdict.status == "invalid":
                await self._invalidate(reason)
        if self.owner.owns_terminal_events:
            notice = {
                "outcome": verdict.status,
                "reason": reason,
                "retry": verdict.status == "failed_infra",
            }
            if self.machine.can("invalidate"):
                await self._advance("invalidate", EventType.TRIAL_INVALIDATED, notice)
            else:
                await self._emit(EventType.TRIAL_INVALIDATED, notice)
        return TrialOutcome(verdict.status, reason=reason, error_class=verdict.error_class)

    async def _invalidate(self, reason: str) -> None:
        decision = evaluate_terminal(isolation_failed=True)
        assert decision is not None
        try:
            for event in event_path_for_terminal(self.machine, decision):
                await self._advance(
                    event, EventType.INCIDENT_OPENED, {"kind": "invalidated", "reason": reason}
                )
        except IllegalTransition:
            return

    async def _teardown(self) -> None:
        """Stop the agent, revoke the key and, when the runner owns them, destroy the sandboxes."""
        owner = self.owner
        ok = True
        if owner.owns_terminal_events:
            await self._emit(EventType.TEARDOWN_STARTED, {"lease_id": self.agent.lease_id})
        if self.agent_runner is not None:
            ok &= await self._guarded("stop agent runner", self.agent_runner.stop())
        if self.key is not None:
            ok &= await self._guarded("revoke trial key", owner.gateway.revoke_key(self.key.key_id))
        if owner.owns_terminal_events:
            seen: set[str] = set()
            for handle in (self.agent, self.leases.agent, self.leases.verifier):
                if handle is not None and handle.lease_id not in seen:
                    seen.add(handle.lease_id)
                    ok &= await self._guarded(
                        f"destroy {handle.role} sandbox", handle.sandbox.destroy()
                    )
            await self._emit(EventType.TEARDOWN_COMPLETED, {"ok": ok})
        if self.scratch_created:
            await asyncio.to_thread(shutil.rmtree, self.scratch, ignore_errors=True)

    async def _guarded(self, step: str, work: Awaitable[object]) -> bool:
        try:
            await work
        except Exception as exc:  # teardown goes on; the failure is recorded as an event
            await self._emit(
                EventType.INCIDENT_OPENED,
                {
                    "kind": "teardown_error",
                    "step": step,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return False
        return True

    # -- events and the record -------------------------------------------------------------------

    async def _advance(self, event: str, carrier: EventType, payload: Mapping[str, Any]) -> None:
        """Apply a state-machine event and append the event that carries the transition."""
        before = self.machine.state
        after = self.machine.apply(event, note=str(payload.get("reason", "")))
        await self._emit(
            carrier,
            {
                "transition": {"event": event, "from": before.value, "to": after.value},
                **payload,
            },
        )

    async def _emit(self, event_type: EventType, payload: Mapping[str, Any]) -> None:
        clock = self.owner.clock
        await self.owner.events.append(
            EventEnvelope(
                event_id=new_event_id(clock.now),
                ts=_iso(clock.now()),
                batch_id=self.trial.batch_id,
                type=event_type,
                actor=ACTOR,
                trial_id=self.trial.trial_id,
                payload=dict(payload),
            )
        )

    def _trial_versions(self, shell: AgentRunner) -> TrialVersions:
        """What this trial runs against (PRD FR-MOD-09), recorded when the agent starts."""
        known = {f.name for f in fields(TrialVersions)}
        seed = {key: value for key, value in self.setup.versions.items() if key in known}
        pack = self.setup.pack
        modes = shell.supported_modes
        template = self.trial.template
        values: dict[str, Any] = {
            "model_version": self.trial.model,
            "freeze_hash": "unfrozen",
            **seed,
            "task_version": pack.version,
            "shell_version": f"{shell.name}@{shell.version}",
            "image_digest": template.digest,
            "rubric_version": pack.rubric.rubric_version,
            "grader_version": f"evalspleef-core@{package_version('evalspleef-core')}",
            "endpoint_id": self.setup.endpoint.endpoint_id,
            "sandbox_adapter": self.owner.providers.sandbox_instance(
                self.agent.provider_instance
            ).name,
            "provider_instance": self.agent.provider_instance,
            "template_digest": template.digest,
            "runner_mode": next(iter(modes)) if len(modes) == 1 else None,
        }
        return TrialVersions(**values)

    def _record_dict(self) -> dict[str, Any]:
        record = self.machine.record
        if self.versions is not None:
            record.extensions["versions"] = dict(self.versions)
        basis = self.setup.versions.get("cost_basis")
        if basis is not None:
            # The price basis of the agent's route, so the report labels the cost (FR-RPT-3).
            record.extensions["cost_basis"] = str(basis)
        verifier = self.leases.verifier
        record.extensions["flow"] = {
            "agent_lease_id": self.agent.lease_id,
            "provider_instance": self.agent.provider_instance,
            "verifier_lease_id": None if verifier is None else verifier.lease_id,
            "endpoint_id": self.setup.endpoint.endpoint_id,
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "stop_reasons": list(self.stop_reasons),
            "feedback": [item.to_dict() for item in self.feedback_records],
            "defects": self.tracker.to_dict(),
            "canary_placements": list(self.placements),
            "preflight": list(self.preflight_log),
            "cheat_signals": [
                {"kind": s.kind, "detail": s.detail, "evidence_uri": s.evidence_uri}
                for s in self.cheat
            ],
            "artifacts": [
                {"path": a.path, "sha256": a.sha256, "size_bytes": a.size_bytes}
                for a in self.artifacts
            ],
        }
        return record.to_dict()
