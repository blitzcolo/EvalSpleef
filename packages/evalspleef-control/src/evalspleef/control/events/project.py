"""Project trial state from the event log and verify projections against the database.

The event log is authoritative and database rows are projections (AGENTS.md
architecture rule 6). :func:`project_trial_state` replays a
:class:`evalspleef.core.state.TrialMachine` from a trial's events, and
:func:`verify_projection` checks that the relational ``trial`` table agrees with
that replay for a whole batch. A trial row whose state has no backing events is
reported as a mismatch: state without events is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.models import TrialState
from evalspleef.core.state import IllegalTransition, TrialMachine

from ..store.db import Database
from ..store.repos import TrialRepository
from .base import EventStore

__all__ = [
    "ProjectionMismatch",
    "ProjectionReport",
    "project_trial_state",
    "verify_projection",
]

#: Default mapping from event type to trial state machine transition. Events
#: not listed here (round bookkeeping, evidence, canary scans, endpoint and
#: host lifecycle, ...) leave the trial state unchanged. A recorded event may
#: override the default by carrying ``payload["transition"]`` with the machine
#: event name.
_EVENT_TRANSITIONS: dict[EventType, str] = {
    EventType.TRIAL_LEASED: "lease",
    EventType.TRIAL_STARTED: "start",
    EventType.AGENT_STOPPED: "stop",
    EventType.FEEDBACK_DECIDED: "needs_feedback",
    EventType.FEEDBACK_SENT: "deliver",
    EventType.VERIFY_STARTED: "finish",
    EventType.TRIAL_SCORED: "score",
}


def project_trial_state(
    events: list[EventEnvelope] | tuple[EventEnvelope, ...],
    *,
    trial_id: str | None = None,
) -> TrialState:
    """Replay ``events`` through :class:`TrialMachine` and return the state.

    Args:
        events: The trial's events in storage (ascending sequence) order.
        trial_id: Which trial to project. Required when ``events`` mixes
            trials; when omitted, the first event carrying a ``trial_id``
            determines it.

    Raises:
        IllegalTransition: the log holds a transition the machine rejects
            (corrupt or out-of-order history).
    """
    machine: TrialMachine | None = None
    resolved_trial_id = trial_id
    for event in events:
        if resolved_trial_id is None and event.trial_id is not None:
            resolved_trial_id = event.trial_id
        if event.trial_id is None or event.trial_id != resolved_trial_id:
            continue
        if event.type is EventType.TRIAL_CREATED:
            machine = TrialMachine(resolved_trial_id)
            continue
        if machine is None:
            # History that starts mid-lifecycle: replay from the pending state.
            machine = TrialMachine(resolved_trial_id)
        transition = _transition_for(event, machine)
        if transition is None:
            continue
        machine.apply(transition, note=str(event.type))
    if machine is None:
        return TrialState.PENDING
    return machine.state


def _transition_for(event: EventEnvelope, machine: TrialMachine) -> str | None:
    """Resolve the machine transition recorded by ``event``, or None."""
    payload_transition = event.payload.get("transition") if event.payload else None
    if isinstance(payload_transition, str):
        return payload_transition
    if event.type is EventType.TRIAL_INVALIDATED:
        # A scored trial is invalidated post hoc; every other state uses the
        # direct invalidate path (docs/02 §4.2).
        return "invalidate_post_hoc" if machine.state is TrialState.SCORED else "invalidate"
    return _EVENT_TRANSITIONS.get(event.type)


@dataclass(frozen=True)
class ProjectionMismatch:
    """One trial whose database state disagrees with the event projection."""

    trial_id: str
    db_state: str
    event_state: str | None
    """None when the trial has no events at all (state without events)."""
    error: str | None = None
    """Set when the event history itself fails to replay."""


@dataclass(frozen=True)
class ProjectionReport:
    """Result of verifying the ``trial`` table against the event log."""

    batch_id: str
    checked: int
    mismatches: tuple[ProjectionMismatch, ...]

    @property
    def ok(self) -> bool:
        """True when every checked trial row matches its event projection."""
        return not self.mismatches


async def verify_projection(store: EventStore, db: Database, *, batch_id: str) -> ProjectionReport:
    """Check every ``trial`` row of ``batch_id`` against the event log.

    A row whose projected state differs from the stored state, whose history
    does not replay, or that has no events at all is a mismatch: the database
    may only ever hold states that the authoritative log derives.
    """
    events = await store.read(batch_id)
    by_trial: dict[str, list[EventEnvelope]] = {}
    for event in events:
        if event.trial_id is not None:
            by_trial.setdefault(event.trial_id, []).append(event)

    trials = await TrialRepository(db).list_by_batch(batch_id)
    mismatches: list[ProjectionMismatch] = []
    for row in trials:
        trial_events = by_trial.get(row.trial_id, [])
        if not trial_events:
            mismatches.append(
                ProjectionMismatch(trial_id=row.trial_id, db_state=row.state, event_state=None)
            )
            continue
        try:
            projected = project_trial_state(trial_events, trial_id=row.trial_id)
        except IllegalTransition as exc:
            mismatches.append(
                ProjectionMismatch(
                    trial_id=row.trial_id,
                    db_state=row.state,
                    event_state=None,
                    error=str(exc),
                )
            )
            continue
        if str(projected) != row.state:
            mismatches.append(
                ProjectionMismatch(
                    trial_id=row.trial_id,
                    db_state=row.state,
                    event_state=str(projected),
                )
            )
    return ProjectionReport(batch_id=batch_id, checked=len(trials), mismatches=tuple(mismatches))
