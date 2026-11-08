"""Batch lifecycle driver: guards, concurrent dispatch, retries and the aggregate report.

The driver walks :class:`evalspleef.core.batch.BatchMachine` (Draft -> Frozen -> Running ->
Graded), runs every planned trial concurrently under the :class:`~..scheduling.Scheduler`, and
hands each one to a :class:`~.executor.TrialExecutor` with leased sandboxes. A trial that ends
``failed_infra`` (including a lost host) stays ``invalid`` and is retried as a new record with an
incremented index, at most three times (PRD appendix B.6). Trial rows, grade rows and events are
written through the control store as outcomes arrive, and ``core.report.BatchReport`` aggregates
the cells at the end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from evalspleef.core.batch import BatchMachine, BatchState
from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.core.models import TrialState, Validity
from evalspleef.core.report import BatchReport, TrialResult
from evalspleef.core.state import IllegalTransition, evaluate_terminal, event_path_for_terminal
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.isolation import IsolationProfile

from ..events import EventStore, replay_trial_machine
from ..leases import LeasedSandbox, LeaseManager, LeaseRequest, LeaseRole
from ..scheduling import (
    CapacityUnavailable,
    HostLoss,
    Reservation,
    ResourceRequest,
    RetryDecision,
    Scheduler,
    SchedulerAlert,
    Ticket,
    WorkItem,
    priority_for,
    retry_decision,
    verifier_requirements,
)
from ..store.repos import GradeRepository, TrialRepository
from ..store.rows import GradeRow, TrialRow
from .executor import LeaseHandle, PlannedTrial, TrialExecutor, TrialLeases, TrialOutcome
from .freeze import FreezeRecord
from .guards import EndpointStates, freeze_guards, run_guards
from .plan import BatchPlan, PlannedCell

__all__ = ["BatchDriver", "BatchRunResult"]

_ACTOR = "batch-driver"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_infra_failure(error: SpleefError) -> bool:
    """Retryable errors and environment losses are infrastructure failures, not model failures."""
    return error.retryable or (
        error.error_class == "environment" and error.disposition == "discard"
    )


def _handle(leased: LeasedSandbox) -> LeaseHandle:
    return LeaseHandle(
        leased.lease_id, leased.role, leased.instance, leased.host_id, leased.sandbox
    )


@dataclass(frozen=True)
class BatchRunResult:
    batch_id: str
    state: BatchState
    version_matrix_hash: str
    report: BatchReport
    alerts: tuple[SchedulerAlert, ...]
    host_losses: tuple[HostLoss, ...]


class BatchDriver:
    """Runs one frozen batch from guards to the aggregate report."""

    def __init__(
        self,
        *,
        plan: BatchPlan,
        freeze: FreezeRecord,
        executor: TrialExecutor,
        scheduler: Scheduler,
        leases: LeaseManager,
        trials: TrialRepository,
        grades: GradeRepository,
        events: EventStore,
        endpoint_states: EndpointStates,
        isolation_profiles: Mapping[str, IsolationProfile],
        now: Callable[[], datetime] = _utcnow,
        deadline_s: float | None = None,
        today: date | None = None,
    ) -> None:
        self._plan = plan
        self._freeze = freeze
        self._executor = executor
        self._scheduler = scheduler
        self._leases = leases
        self._trials = trials
        self._grades = grades
        self._events = events
        self._endpoint_states = endpoint_states
        self._profiles = isolation_profiles
        self._now = now
        self._deadline_s = deadline_s
        self._today = today
        self._machine = BatchMachine()
        self._meta: dict[str, tuple[str, str | None]] = {}
        """Trial id -> (created_at, retry_of)."""
        self._host_losses: list[HostLoss] = []

    @property
    def state(self) -> BatchState:
        return self._machine.state

    # -- lifecycle ---------------------------------------------------------------------

    def freeze(self, *, lint_passed: bool, budget_approved: bool) -> dict[str, Any]:
        """Draft -> Frozen; raises ``BatchGuardFailed`` listing every unmet FR-FLOW-01 guard."""
        guards = freeze_guards(
            self._plan, self._freeze, lint_passed=lint_passed, budget_approved=budget_approved
        )
        return self._machine.move(BatchState.FROZEN, freeze_guards=guards)

    async def start(self) -> dict[str, Any]:
        """Frozen -> Running; raises ``BatchGuardFailed`` unless the FR-FLOW-02 guards hold."""
        guards = await run_guards(
            self._plan,
            endpoint_states=self._endpoint_states,
            capacity=self._scheduler.capacity,
            profiles=self._profiles,
            deadline_s=self._deadline_s,
            today=self._today,
        )
        return self._machine.move(BatchState.RUNNING, run_guards=guards)

    async def run(self) -> BatchRunResult:
        """Run every trial, move to Graded and aggregate the report."""
        if self._machine.state != BatchState.RUNNING:
            await self.start()
        results = await self._run_trials()
        self._machine.move(BatchState.GRADED)
        report = BatchReport(
            batch_id=self._plan.batch_id,
            trials=tuple(results),
            skips=self._plan.skips(),
            generated_at=self._ts(),
        )
        return BatchRunResult(
            batch_id=self._plan.batch_id,
            state=self._machine.state,
            version_matrix_hash=self._freeze.version_matrix_hash,
            report=report,
            alerts=tuple(self._scheduler.alerts),
            host_losses=tuple(self._host_losses),
        )

    # -- host loss ---------------------------------------------------------------------

    async def host_lost(self, instance: str, host_id: str) -> HostLoss:
        """Remove a host; its running trials fail as ``failed_infra`` and are retried."""
        loss = self._scheduler.host_lost(instance, host_id)
        await self._record_host_loss(loss)
        return loss

    async def sweep_hosts(self) -> list[HostLoss]:
        """Drain every host whose heartbeat went stale."""
        losses = self._scheduler.sweep_stale_hosts()
        for loss in losses:
            await self._record_host_loss(loss)
        return losses

    async def _record_host_loss(self, loss: HostLoss) -> None:
        self._host_losses.append(loss)
        await self._emit(
            EventType.HOST_DRAINED,
            None,
            {
                "instance": loss.instance,
                "host_id": loss.host_id,
                "trial_ids": list(loss.trial_ids),
            },
        )

    # -- dispatch ----------------------------------------------------------------------

    async def _run_trials(self) -> list[TrialResult]:
        queued: list[tuple[PlannedTrial, Ticket]] = []
        for trial in self._plan.trials:
            await self._create_record(trial, retry_of=None)
            queued.append((trial, self._scheduler.enqueue(self._work_item(trial))))
        # One dispatch pass over the whole queue, so fairness and priority apply to all of it.
        self._scheduler.dispatch()
        async with asyncio.TaskGroup() as group:
            chains = [group.create_task(self._chain(trial, ticket)) for trial, ticket in queued]
        results = [result for chain in chains for result in chain.result()]
        results.extend(self._skipped_result(cell) for cell in self._plan.skipped_cells)
        return results

    def _work_item(self, trial: PlannedTrial) -> WorkItem:
        requirements = trial.requirements
        return WorkItem(
            trial_id=trial.trial_id,
            cell_id=trial.cell_id,
            model=trial.model,
            routes=trial.agent_routes,
            request=ResourceRequest(requirements.cpus, requirements.memory_mb),
            priority=priority_for(requirements, rerun=trial.attempt > 0),
        )

    async def _chain(self, trial: PlannedTrial, ticket: Ticket) -> list[TrialResult]:
        """Run a trial and, while it fails on infrastructure, its retries; one result each."""
        results: list[TrialResult] = []
        while True:
            outcome = await self._attempt(trial, ticket)
            decision = (
                retry_decision(trial.trial_id, trial.attempt)
                if outcome.status == "failed_infra"
                else None
            )
            await self._finish(trial, outcome, decision)
            results.append(TrialResult.from_record(self._record_of(trial, outcome)))
            if decision is None or not decision.retry or decision.next_trial_id is None:
                return results
            previous = trial.trial_id
            trial = replace(
                trial,
                trial_id=decision.next_trial_id,
                index=trial.index + 1,
                attempt=decision.attempt,
            )
            await self._create_record(trial, retry_of=previous)
            ticket = self._scheduler.enqueue(self._work_item(trial))
            self._scheduler.dispatch()

    async def _attempt(self, trial: PlannedTrial, ticket: Ticket) -> TrialOutcome:
        try:
            reservation = await ticket.wait()
        except CapacityUnavailable as exc:
            return TrialOutcome("invalid", reason=f"CapacityUnavailable: {exc.message}")
        except asyncio.CancelledError:
            self._scheduler.cancel(ticket)
            raise
        try:
            return await self._run_leased(trial, reservation)
        except SpleefError as exc:
            reason = f"{type(exc).__name__}: {exc.message}"
            if _is_infra_failure(exc):
                return TrialOutcome("failed_infra", reason=reason)
            return TrialOutcome("invalid", reason=reason)
        finally:
            # Sandboxes are already destroyed by now, so the provider quota is free again.
            self._scheduler.release(reservation)

    async def _run_leased(self, trial: PlannedTrial, reservation: Reservation) -> TrialOutcome:
        async with AsyncExitStack() as stack:
            agent = await stack.enter_async_context(
                self._leases.lease(
                    self._lease_request(trial, "agent", reservation.instance, reservation.host_id)
                )
            )
            await self._trials.upsert(self._row(trial, TrialState.LEASED, Validity.PENDING))

            async def open_verifier() -> LeaseHandle:
                if reservation.instance in trial.verifier_routes:
                    instance, host_id = reservation.instance, reservation.host_id
                else:
                    instance, host_id = trial.verifier_routes[0], None
                verifier = await stack.enter_async_context(
                    self._leases.lease(self._lease_request(trial, "verifier", instance, host_id))
                )
                return _handle(verifier)

            leases = TrialLeases(_handle(agent), open_verifier)
            return await self._race_host_loss(trial, leases, reservation)

    async def _race_host_loss(
        self, trial: PlannedTrial, leases: TrialLeases, reservation: Reservation
    ) -> TrialOutcome:
        """Run the executor, but fail the attempt as soon as its host is lost."""
        run = asyncio.ensure_future(self._executor.run_trial(trial, leases))
        lost = asyncio.ensure_future(reservation.lost.wait())
        try:
            await asyncio.wait({run, lost}, return_when=asyncio.FIRST_COMPLETED)
            if run.done():
                return run.result()
            return TrialOutcome(
                "failed_infra",
                reason=f"host lost: {reservation.instance}/{reservation.host_id}",
            )
        finally:
            run.cancel()
            lost.cancel()
            await asyncio.gather(run, lost, return_exceptions=True)

    def _lease_request(
        self, trial: PlannedTrial, role: LeaseRole, instance: str, host_id: str | None
    ) -> LeaseRequest:
        requirements = (
            trial.requirements if role == "agent" else verifier_requirements(trial.requirements)
        )
        return LeaseRequest(
            batch_id=trial.batch_id,
            trial_id=trial.trial_id,
            role=role,
            instance=instance,
            requirements=requirements,
            template=trial.template,
            host_id=host_id,
        )

    # -- bookkeeping -------------------------------------------------------------------

    def _ts(self) -> str:
        return _iso(self._now())

    def _row(
        self,
        trial: PlannedTrial,
        state: TrialState,
        validity: Validity,
        versions: Mapping[str, Any] | None = None,
    ) -> TrialRow:
        created_at, retry_of = self._meta[trial.trial_id]
        extensions: dict[str, Any] = {
            "freeze_hash": self._freeze.version_matrix_hash,
            "attempt": trial.attempt,
            "retry_of": retry_of,
            "endpoint_id": trial.endpoint_id,
        }
        if versions is not None:
            extensions["versions"] = dict(versions)
        return TrialRow(
            trial_id=trial.trial_id,
            batch_id=trial.batch_id,
            task_id=trial.task_id,
            model=trial.model,
            shell=trial.shell,
            state=str(state),
            validity=str(validity),
            created_at=created_at,
            updated_at=self._ts(),
            cell=trial.cell_id,
            epoch=trial.epoch,
            idx=trial.index,
            extensions=extensions,
        )

    async def _create_record(self, trial: PlannedTrial, *, retry_of: str | None) -> None:
        self._meta[trial.trial_id] = (self._ts(), retry_of)
        await self._trials.upsert(self._row(trial, TrialState.PENDING, Validity.PENDING))
        await self._emit(
            EventType.TRIAL_CREATED,
            trial.trial_id,
            {
                "cell_id": trial.cell_id,
                "epoch": trial.epoch,
                "index": trial.index,
                "attempt": trial.attempt,
                "retry_of": retry_of,
                "freeze_hash": self._freeze.version_matrix_hash,
            },
        )

    def _record_of(self, trial: PlannedTrial, outcome: TrialOutcome) -> Mapping[str, Any]:
        """The trial record JSON for ``outcome``, validated against the planned trial."""
        record = outcome.record
        if outcome.status in ("scored", "paused_manual") and record is not None:
            if record.get("trial_id") != trial.trial_id:
                raise ValueError(
                    f"executor returned a record for {record.get('trial_id')!r}, "
                    f"expected {trial.trial_id!r}"
                )
            return record
        if outcome.status == "scored":
            raise ValueError(f"a scored outcome of {trial.trial_id} needs a trial record")
        paused = outcome.status == "paused_manual"
        return {
            "trial_id": trial.trial_id,
            "state": str(TrialState.PAUSED_MANUAL if paused else TrialState.INVALID),
            "validity": str(Validity.MANUAL if paused else Validity.INVALID),
            "grades": [],
        }

    async def _settle_invalid(self, trial: PlannedTrial, notice: Mapping[str, Any]) -> TrialState:
        """Move a failed trial to ``invalid`` with explicit transitions; return its state.

        The trial's state is replayed from the event log (the authority), then
        the legal path to ``invalid`` is walked one event per transition. A
        trial left in ``observing`` or ``awaiting_feedback`` has no legal path
        (appendix B.2); it keeps that state and only the notice is recorded.
        """
        machine = replay_trial_machine(
            await self._events.read(self._plan.batch_id, trial_id=trial.trial_id),
            trial_id=trial.trial_id,
        )
        decision = evaluate_terminal(isolation_failed=True)
        assert decision is not None
        path: tuple[str, ...] = ()
        if machine.state is not TrialState.INVALID:
            try:
                path = event_path_for_terminal(machine, decision)
            except IllegalTransition:
                path = ()
        for position, event in enumerate(path):
            before = machine.state
            after = machine.apply(event, note=str(notice.get("reason", "")))
            transition = {"event": event, "from": before.value, "to": after.value}
            last = position == len(path) - 1
            await self._emit(
                EventType.TRIAL_INVALIDATED if last else EventType.INCIDENT_OPENED,
                trial.trial_id,
                {"transition": transition, **notice}
                if last
                else {"transition": transition, "kind": "settled", "reason": notice.get("reason")},
            )
        if not path:
            await self._emit(EventType.TRIAL_INVALIDATED, trial.trial_id, dict(notice))
        return machine.state

    async def _projected_state(self, trial: PlannedTrial) -> TrialState:
        events = await self._events.read(self._plan.batch_id, trial_id=trial.trial_id)
        return replay_trial_machine(events, trial_id=trial.trial_id).state

    async def _started_versions(self, trial: PlannedTrial) -> Mapping[str, Any] | None:
        """``trial.versions`` as the executor recorded it when the agent started, if it did."""
        events = await self._events.read(self._plan.batch_id, trial_id=trial.trial_id)
        started = [e for e in events if e.type is EventType.TRIAL_STARTED]
        versions = started[-1].payload.get("versions") if started else None
        return versions if isinstance(versions, Mapping) else None

    async def _finish(
        self, trial: PlannedTrial, outcome: TrialOutcome, retry: RetryDecision | None
    ) -> None:
        """Project the outcome into the trial row, grade rows and the terminal event.

        Row states are projections of the event log (AGENTS.md rule 6): the
        executor records the trial's transitions, and the row takes the state
        that the log replays to.
        """
        if outcome.status == "scored":
            record = self._record_of(trial, outcome)
            grades = list(record.get("grades") or ())
            state = await self._projected_state(trial)
            versions = await self._started_versions(trial)
            await self._trials.upsert(self._row(trial, state, Validity.VALID, versions))
            for position, grade in enumerate(grades, start=1):
                await self._grades.insert(
                    GradeRow(
                        trial_id=trial.trial_id,
                        version=int(grade.get("grade_version", position)),
                        tier=str(grade["tier"]),
                        status=str(grade.get("status", "final")),
                        decided_by=str(grade.get("decided_by", "auto")),
                        payload=dict(grade),
                        created_at=self._ts(),
                        rule_id=(grade.get("decision") or {}).get("rule_id"),
                    )
                )
            await self._emit(
                EventType.TRIAL_SCORED,
                trial.trial_id,
                {"tier": str(grades[-1]["tier"]) if grades else None, "grades": len(grades)},
            )
        elif outcome.status == "paused_manual":
            state = await self._projected_state(trial)
            versions = await self._started_versions(trial)
            await self._trials.upsert(self._row(trial, state, Validity.MANUAL, versions))
        else:
            state = await self._settle_invalid(
                trial,
                {
                    "outcome": outcome.status,
                    "reason": outcome.reason,
                    "retry": retry.retry if retry is not None else False,
                    "next_trial_id": retry.next_trial_id if retry is not None else None,
                },
            )
            versions = await self._started_versions(trial)
            await self._trials.upsert(self._row(trial, state, Validity.INVALID, versions))

    def _skipped_result(self, cell: PlannedCell) -> TrialResult:
        """Placeholder so a skipped cell shows up in the report as Skip(<reason>)."""
        return TrialResult.from_record(
            {
                "trial_id": f"{cell.cell_id}.e1.t01",
                "state": str(TrialState.PENDING),
                "validity": str(Validity.PENDING),
                "grades": [],
            }
        )

    async def _emit(
        self, event_type: EventType, trial_id: str | None, payload: Mapping[str, Any]
    ) -> None:
        await self._events.append(
            EventEnvelope(
                event_id=new_event_id(),
                ts=self._ts(),
                batch_id=self._plan.batch_id,
                type=event_type,
                actor=_ACTOR,
                trial_id=trial_id,
                payload=dict(payload),
            )
        )
