"""Operator actions on finished trials: regrade, void and manual resolution (PRD appendix B.6/B.7).

Every action appends events first and then updates the projections, so the
event log stays authoritative (AGENTS.md rule 6):

* ``regrade`` re-runs the pure grading on the inputs recorded with the first
  grade and appends a new grade version; the agent is not run again.
* ``void`` walks the legal path to ``invalid`` (a scored trial is invalidated
  post hoc), one event per transition.
* ``resolve`` records a human decision: a paused trial resumes verification
  and is scored with the human grade; a scored trial gets an overriding version.
* ``expire_paused`` invalidates trials left paused longer than the manual
  intervention window (P5, docs/02 section 6).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.core.grade_inputs import GradingInputs
from evalspleef.core.models import (
    Cost,
    DecidedBy,
    Decision,
    Grade,
    GradeStatus,
    HumanOverride,
    RuleId,
    Tier,
    TrialState,
    Validity,
)
from evalspleef.core.state import IllegalTransition, evaluate_terminal, event_path_for_terminal

from ..events import EventStore, replay_trial_machine
from ..store.repos import GradeRepository, TrialRepository
from ..store.rows import GradeRow, TrialRow

__all__ = ["OperationRefused", "TrialOperations", "recorded_inputs"]

_ACTOR = "operator"
MANUAL_PAUSE_LIMIT_S = 1800
"""P5: a trial paused for a human for 30 minutes without a decision is invalid (docs/02 6)."""


def _instant(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class OperationRefused(Exception):
    """The trial's state or history does not allow the requested action."""


@dataclass
class TrialOperations:
    events: EventStore
    trials: TrialRepository
    grades: GradeRepository
    now: Callable[[], str]

    async def regrade(self, trial_id: str, *, reason: str) -> GradeRow:
        row, log = await self._load(trial_id)
        if row.state != str(TrialState.SCORED):
            raise OperationRefused(f"{trial_id} is {row.state}; only a scored trial is regraded")
        inputs = recorded_inputs(log)
        if inputs is None:
            raise OperationRefused(f"{trial_id} has no recorded grading inputs to regrade from")
        outcome = inputs.grade(decided_at=self.now())
        if outcome.grade is None:
            reasons = "; ".join(outcome.manual_reasons) or "no grade produced"
            raise OperationRefused(f"{trial_id} now needs a human decision: {reasons}")
        grade = outcome.grade
        grade.manual_notes = f"regrade: {reason}"
        stored = await self._append_grade(row, grade, rule_id=outcome.rule_id)
        await self._emit(
            row,
            EventType.GRADE_COMPUTED,
            {
                "regrade": True,
                "reason": reason,
                "rule_id": outcome.rule_id,
                "tier": stored.tier,
                "penalty": grade.penalty,
                "grade": dict(stored.payload),
                "inputs": inputs.to_dict(),
            },
        )
        return stored

    async def void(self, trial_id: str, *, reason: str) -> TrialState:
        row, log = await self._load(trial_id)
        machine = replay_trial_machine(log, trial_id=trial_id)
        if machine.state is TrialState.INVALID:
            raise OperationRefused(f"{trial_id} is already invalid")
        decision = evaluate_terminal(isolation_failed=True)
        assert decision is not None
        try:
            path = event_path_for_terminal(machine, decision)
        except IllegalTransition as exc:
            raise OperationRefused(f"{trial_id} cannot be voided from {machine.state}") from exc
        for position, event in enumerate(path):
            before = machine.state
            after = machine.apply(event, note=reason)
            last = position == len(path) - 1
            await self._emit(
                row,
                EventType.TRIAL_INVALIDATED if last else EventType.INCIDENT_OPENED,
                {
                    "transition": {"event": event, "from": before.value, "to": after.value},
                    "kind": "voided",
                    "reason": reason,
                    "retry": False,
                },
            )
        await self.trials.upsert(
            replace(
                row, state=str(machine.state), validity=str(Validity.INVALID), updated_at=self.now()
            )
        )
        return machine.state

    async def resolve(
        self, trial_id: str, *, tier: str, penalty: int, reviewer: str, reason: str
    ) -> GradeRow:
        if len(reason) < 8:
            raise OperationRefused("a human decision needs a reason of at least 8 characters")
        row, log = await self._load(trial_id)
        machine = replay_trial_machine(log, trial_id=trial_id)
        if machine.state not in (TrialState.PAUSED_MANUAL, TrialState.SCORED):
            raise OperationRefused(
                f"{trial_id} is {machine.state}; only a paused or scored trial takes a decision"
            )
        decided_at = self.now()
        grade = self._human_grade(row, log, decided_at, tier=Tier(tier), penalty=penalty)
        existing = await self.grades.list_by_trial(trial_id)
        grade.human_override = HumanOverride(
            reviewer=reviewer,
            reason=reason,
            at=decided_at,
            previous_tier=Tier(existing[-1].tier) if existing else None,
        )
        paused = machine.state is TrialState.PAUSED_MANUAL
        if paused:
            before = machine.state
            after = machine.apply("resume_verifying", note=reason)
            await self._emit(
                row,
                EventType.INCIDENT_OPENED,
                {
                    "transition": {
                        "event": "resume_verifying",
                        "from": before.value,
                        "to": after.value,
                    },
                    "kind": "manual_resolution",
                    "reviewer": reviewer,
                    "reason": reason,
                },
            )
        stored = await self._append_grade(row, grade, rule_id=str(RuleId.HUMAN_99))
        payload: dict[str, Any] = {
            "rule_id": str(RuleId.HUMAN_99),
            "tier": stored.tier,
            "penalty": penalty,
            "grade": dict(stored.payload),
            "reviewer": reviewer,
            "reason": reason,
        }
        if paused:  # resumed into verifying above; the human grade scores it
            before = machine.state
            after = machine.apply("score", note=reason)
            payload["transition"] = {"event": "score", "from": before.value, "to": after.value}
        await self._emit(row, EventType.GRADE_COMPUTED, payload)
        await self.trials.upsert(
            replace(
                row, state=str(machine.state), validity=str(Validity.VALID), updated_at=decided_at
            )
        )
        return stored

    async def expire_paused(
        self, batch_id: str, *, max_pause_s: int = MANUAL_PAUSE_LIMIT_S
    ) -> list[str]:
        """Invalidate the batch's trials paused longer than ``max_pause_s``; their ids."""
        now = _instant(self.now())
        expired: list[str] = []
        for row in await self.trials.list_by_batch(batch_id):
            if row.state != str(TrialState.PAUSED_MANUAL):
                continue
            log = await self.events.read(batch_id, trial_id=row.trial_id)
            machine = replay_trial_machine(log, trial_id=row.trial_id)
            paused_at = [
                event.ts
                for event in log
                if (event.payload.get("transition") or {}).get("to")
                == str(TrialState.PAUSED_MANUAL)
            ]
            if machine.state is not TrialState.PAUSED_MANUAL or not paused_at:
                continue
            waited = (now - _instant(paused_at[-1])).total_seconds()
            if waited < max_pause_s:
                continue
            decision = evaluate_terminal(manual_interrupt_expired=True)
            assert decision is not None
            reason = f"paused {int(waited)} s without a human decision (limit {max_pause_s} s)"
            path = event_path_for_terminal(machine, decision)
            for position, event in enumerate(path):
                before = machine.state
                after = machine.apply(event, note=reason)
                last = position == len(path) - 1
                await self._emit(
                    row,
                    EventType.TRIAL_INVALIDATED if last else EventType.INCIDENT_OPENED,
                    {
                        "transition": {"event": event, "from": before.value, "to": after.value},
                        "kind": "manual_timeout",
                        "code": decision.code,
                        "reason": reason,
                        "retry": False,
                    },
                )
            await self.trials.upsert(
                replace(
                    row,
                    state=str(machine.state),
                    validity=str(Validity.INVALID),
                    updated_at=self.now(),
                )
            )
            expired.append(row.trial_id)
        return expired

    # -- helpers -------------------------------------------------------------------------

    async def _load(self, trial_id: str) -> tuple[TrialRow, list[EventEnvelope]]:
        row = await self.trials.get(trial_id)
        if row is None:
            raise OperationRefused(f"no trial {trial_id!r} in the result store")
        return row, await self.events.read(row.batch_id, trial_id=trial_id)

    def _human_grade(
        self,
        row: TrialRow,
        log: list[EventEnvelope],
        decided_at: str,
        *,
        tier: Tier,
        penalty: int,
    ) -> Grade:
        """The human grade keeps the recorded summaries and cost; tier and penalty are the human's."""
        decision = Decision.for_rule(
            RuleId.HUMAN_99, f"human decision: tier {tier}, penalty {penalty}"
        )
        inputs = recorded_inputs(log)
        computed = inputs.grade(decided_at=decided_at).grade if inputs is not None else None
        if computed is not None:
            grade = computed
        else:
            grade = Grade(
                trial_id=row.trial_id, tier=tier, decision=decision, decided_at=decided_at
            )
            grade.cost = _recorded_cost(log)
        grade.tier = tier
        grade.penalty = penalty
        grade.decision = decision
        grade.decided_by = DecidedBy.HUMAN
        grade.status = GradeStatus.FINAL
        grade.validity = Validity.VALID
        return grade

    async def _append_grade(self, row: TrialRow, grade: Grade, *, rule_id: str | None) -> GradeRow:
        existing = await self.grades.list_by_trial(row.trial_id)
        grade.grade_version = max((g.version for g in existing), default=0) + 1
        grade.supersedes = existing[-1].version if existing else None
        stored = GradeRow(
            trial_id=row.trial_id,
            version=grade.grade_version,
            tier=str(grade.tier),
            status=str(grade.status),
            decided_by=str(grade.decided_by),
            payload=grade.to_dict(),
            created_at=self.now(),
            rule_id=rule_id,
        )
        await self.grades.insert(stored)
        return stored

    async def _emit(self, row: TrialRow, event_type: EventType, payload: Mapping[str, Any]) -> None:
        await self.events.append(
            EventEnvelope(
                event_id=new_event_id(),
                ts=self.now(),
                batch_id=row.batch_id,
                type=event_type,
                actor=_ACTOR,
                trial_id=row.trial_id,
                payload=dict(payload),
            )
        )


def recorded_inputs(log: list[EventEnvelope]) -> GradingInputs | None:
    """The grading inputs of the latest grade or manual-review pause that recorded them."""
    for event in reversed(log):
        inputs = event.payload.get("inputs") if event.payload else None
        if isinstance(inputs, Mapping):
            return GradingInputs.from_dict(inputs)
    return None


def _recorded_cost(log: list[EventEnvelope]) -> Cost:
    for event in reversed(log):
        cost = event.payload.get("cost") if event.payload else None
        if isinstance(cost, Mapping):
            return Cost(
                agent=float(cost.get("agent", 0.0)),
                judge=float(cost.get("judge", 0.0)),
                user=float(cost.get("user", 0.0)),
            )
    return Cost()
