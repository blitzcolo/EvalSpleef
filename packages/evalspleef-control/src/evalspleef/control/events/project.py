"""Project trial state from the event log and verify projections against the database.

The event log is authoritative and database rows are projections (AGENTS.md
architecture rule 6). :func:`project_trial_state` replays a
:class:`evalspleef.core.state.TrialMachine` from a trial's events, and
:func:`verify_projection` checks that the relational ``trial`` table agrees with
that replay for a whole batch. A trial row whose state has no backing events is
reported as a mismatch: state without events is a bug.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.models import TrialState
from evalspleef.core.state import IllegalTransition, TrialMachine

from ..store.db import Database
from ..store.repos import TrialRepository
from .base import EventStore

__all__ = [
    "MalformedTransition",
    "ProjectionMismatch",
    "ProjectionReport",
    "project_trial_state",
    "replay_trial_machine",
    "verify_projection",
]


class MalformedTransition(ValueError):
    """An event's ``payload["transition"]`` is neither a machine event name nor a record of one."""


def project_trial_state(
    events: list[EventEnvelope] | tuple[EventEnvelope, ...],
    *,
    trial_id: str | None = None,
) -> TrialState:
    """Replay ``events`` through :class:`TrialMachine` and return the state."""
    return replay_trial_machine(events, trial_id=trial_id).state


def replay_trial_machine(
    events: list[EventEnvelope] | tuple[EventEnvelope, ...],
    *,
    trial_id: str | None = None,
) -> TrialMachine:
    """Replay ``events`` through a fresh :class:`TrialMachine` and return it.

    Only events that carry an explicit ``payload["transition"]`` change the
    state: either the machine event name or the ``{"event", "from", "to"}``
    record the trial runner writes. Every other event (round bookkeeping,
    evidence, canary scans, terminal notices, endpoint and host lifecycle) is
    informational, so one state change is carried by exactly one event.

    Args:
        events: The trial's events in storage (ascending sequence) order.
        trial_id: Which trial to project. Required when ``events`` mixes
            trials; when omitted, the first event carrying a ``trial_id``
            determines it.

    Raises:
        IllegalTransition: the log holds a transition the machine rejects
            (corrupt or out-of-order history).
        MalformedTransition: an event's transition field has an unknown shape.
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
        transition = _transition_for(event)
        if transition is None:
            continue
        machine.apply(transition, note=str(event.type))
    return machine if machine is not None else TrialMachine(resolved_trial_id or "")


def _transition_for(event: EventEnvelope) -> str | None:
    """The machine event recorded by ``event``, or None for an informational event."""
    recorded = event.payload.get("transition") if event.payload else None
    if recorded is None:
        return None
    if isinstance(recorded, str):
        return recorded
    if isinstance(recorded, Mapping) and isinstance(recorded.get("event"), str):
        return str(recorded["event"])
    raise MalformedTransition(f"event {event.event_id} carries transition {recorded!r}")


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
        except (IllegalTransition, MalformedTransition) as exc:
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
