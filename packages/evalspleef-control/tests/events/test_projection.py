"""Projection tests: replaying the trial state machine from events, and verifying the DB projection."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio

from evalspleef.control.events import (
    JsonlEventStore,
    project_trial_state,
    verify_projection,
)
from evalspleef.control.store import TrialRepository, TrialRow, connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.core.state import TrialMachine

TRIAL = "web-001.demo.model.shell.e1.t01"
BATCH = "2026-11-b1"
MOMENT = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def make_event(
    event_type: EventType,
    *,
    trial_id: str | None = TRIAL,
    payload: dict[str, object] | None = None,
    salt: int = 0,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=new_event_id(clock=lambda: MOMENT, rng=random.Random(salt)),
        ts=MOMENT.isoformat(),
        batch_id=BATCH,
        trial_id=trial_id,
        type=event_type,
        actor="controller",
        payload=payload or {},
    )


def step(
    machine: TrialMachine, event_type: EventType, event: str, salt: int, **extra: object
) -> EventEnvelope:
    """Apply ``event`` to ``machine`` and return the event the trial runner would append."""
    before = machine.state
    after = machine.apply(event)
    transition = {"event": event, "from": before.value, "to": after.value}
    return make_event(event_type, payload={"transition": transition, **extra}, salt=salt)


def test_projection_equals_live_machine() -> None:
    """Replay the runner's recorded transitions through the machine and through events."""
    machine = TrialMachine(TRIAL)
    events: list[EventEnvelope] = [make_event(EventType.TRIAL_CREATED, salt=0)]
    transitions = [
        (EventType.TRIAL_LEASED, "lease"),
        (EventType.TRIAL_STARTED, "start"),
        (EventType.AGENT_STOPPED, "stop"),
        (EventType.DEFECT_UPDATED, "needs_feedback"),
        (EventType.FEEDBACK_SENT, "deliver"),
        (EventType.AGENT_STOPPED, "stop"),
        (EventType.VERIFY_STARTED, "finish"),
        (EventType.GRADE_COMPUTED, "score"),
    ]
    for salt, (event_type, transition) in enumerate(transitions, start=1):
        events.append(step(machine, event_type, transition, salt))
    # Events without a transition do not drive the trial state machine.
    events.append(make_event(EventType.EVIDENCE_COLLECTED, payload={"path": "/tmp/x"}, salt=99))

    assert project_trial_state(events, trial_id=TRIAL) is machine.state
    assert machine.state.value == "scored"


def test_notices_without_a_transition_do_not_move_the_state() -> None:
    """The runner also appends feedback decisions, verify notices and the scored notice.

    These carry no transition; treating them as transitions applied ``needs_feedback``,
    ``finish`` and ``score`` a second time and broke the replay.
    """
    machine = TrialMachine(TRIAL)
    events = [
        make_event(EventType.TRIAL_CREATED, salt=0),
        step(machine, EventType.TRIAL_LEASED, "lease", 1),
        step(machine, EventType.TRIAL_STARTED, "start", 2),
        step(machine, EventType.AGENT_STOPPED, "stop", 3),
        step(machine, EventType.DEFECT_UPDATED, "needs_feedback", 4),
        make_event(EventType.FEEDBACK_DECIDED, payload={"feedback_index": 1}, salt=5),
        step(machine, EventType.FEEDBACK_SENT, "deliver", 6),
        step(machine, EventType.AGENT_STOPPED, "stop", 7),
        step(machine, EventType.VERIFY_STARTED, "finish", 8),
        make_event(EventType.VERIFY_STARTED, payload={"reason": "retry"}, salt=9),
        step(machine, EventType.GRADE_COMPUTED, "score", 10),
        make_event(EventType.TRIAL_SCORED, payload={"tier": "A"}, salt=11),
    ]
    assert project_trial_state(events, trial_id=TRIAL).value == "scored"


def test_projection_accepts_a_bare_event_name() -> None:
    machine = TrialMachine(TRIAL)
    machine.apply("lease")
    machine.apply("fail_infra")
    events = [
        make_event(EventType.TRIAL_CREATED, salt=0),
        make_event(EventType.TRIAL_LEASED, payload={"transition": "lease"}, salt=1),
        make_event(EventType.INCIDENT_OPENED, payload={"transition": "fail_infra"}, salt=2),
    ]
    projected = project_trial_state(events, trial_id=TRIAL)
    assert projected is machine.state
    assert projected.value == "failed_infra"


def test_projection_of_a_failure_then_invalidation() -> None:
    """The runner's fail_infra followed by the driver's invalidation replays to invalid."""
    machine = TrialMachine(TRIAL)
    events = [
        make_event(EventType.TRIAL_CREATED, salt=0),
        step(machine, EventType.TRIAL_LEASED, "lease", 1),
        step(machine, EventType.TRIAL_STARTED, "start", 2),
        step(machine, EventType.INCIDENT_OPENED, "fail_infra", 3),
        step(machine, EventType.TRIAL_INVALIDATED, "invalidate", 4, retry=True),
    ]
    assert project_trial_state(events, trial_id=TRIAL).value == "invalid"


def test_projection_of_scored_then_invalidated() -> None:
    machine = TrialMachine(TRIAL)
    events = [make_event(EventType.TRIAL_CREATED, salt=0)]
    path = [
        (EventType.TRIAL_LEASED, "lease"),
        (EventType.TRIAL_STARTED, "start"),
        (EventType.AGENT_STOPPED, "stop"),
        (EventType.VERIFY_STARTED, "finish"),
        (EventType.GRADE_COMPUTED, "score"),
        (EventType.CANARY_SCANNED, "invalidate_post_hoc"),
    ]
    for salt, (event_type, transition) in enumerate(path, start=1):
        events.append(step(machine, event_type, transition, salt))
    events.append(make_event(EventType.TRIAL_INVALIDATED, payload={"reason": "canary"}, salt=9))
    assert project_trial_state(events, trial_id=TRIAL) is machine.state
    assert machine.state.value == "invalid"


def test_projection_without_events_is_pending() -> None:
    assert project_trial_state([], trial_id=TRIAL).value == "pending"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


async def test_verify_projection_matches(db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = JsonlEventStore(tmp_path, fsync=False)
    repo = TrialRepository(db)

    # Trial written by a correct projection pipeline.
    await repo.upsert(
        TrialRow(
            trial_id=TRIAL,
            batch_id=BATCH,
            task_id="web-001",
            model="demo",
            shell="shell",
            state="scored",
            validity="valid",
            created_at=MOMENT.isoformat(),
            updated_at=MOMENT.isoformat(),
        )
    )
    machine = TrialMachine(TRIAL)
    await store.append(make_event(EventType.TRIAL_CREATED, salt=0))
    for salt, (event_type, transition) in enumerate(
        [
            (EventType.TRIAL_LEASED, "lease"),
            (EventType.TRIAL_STARTED, "start"),
            (EventType.AGENT_STOPPED, "stop"),
            (EventType.VERIFY_STARTED, "finish"),
            (EventType.GRADE_COMPUTED, "score"),
        ],
        start=1,
    ):
        await store.append(step(machine, event_type, transition, salt))

    report = await verify_projection(store, db, batch_id=BATCH)
    assert report.ok
    assert report.checked == 1


async def test_verify_projection_reports_a_malformed_transition(db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = JsonlEventStore(tmp_path, fsync=False)
    await TrialRepository(db).upsert(
        TrialRow(
            trial_id=TRIAL,
            batch_id=BATCH,
            task_id="web-001",
            model="demo",
            shell="shell",
            state="leased",
            validity="pending",
            created_at=MOMENT.isoformat(),
            updated_at=MOMENT.isoformat(),
        )
    )
    await store.append(make_event(EventType.TRIAL_CREATED, salt=0))
    await store.append(make_event(EventType.TRIAL_LEASED, payload={"transition": 7}, salt=1))
    report = await verify_projection(store, db, batch_id=BATCH)
    assert not report.ok
    assert report.mismatches[0].error is not None


async def test_verify_projection_reports_state_without_events(db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = JsonlEventStore(tmp_path, fsync=False)
    repo = TrialRepository(db)
    await repo.upsert(
        TrialRow(
            trial_id=TRIAL,
            batch_id=BATCH,
            task_id="web-001",
            model="demo",
            shell="shell",
            state="leased",
            validity="pending",
            created_at=MOMENT.isoformat(),
            updated_at=MOMENT.isoformat(),
        )
    )
    report = await verify_projection(store, db, batch_id=BATCH)
    assert not report.ok
    assert len(report.mismatches) == 1
    mismatch = report.mismatches[0]
    assert mismatch.trial_id == TRIAL
    assert mismatch.event_state is None  # state without events is a bug


async def test_verify_projection_reports_diverged_state(db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = JsonlEventStore(tmp_path, fsync=False)
    repo = TrialRepository(db)
    # The DB says "leased" but the log only has trial.created (pending).
    await repo.upsert(
        TrialRow(
            trial_id=TRIAL,
            batch_id=BATCH,
            task_id="web-001",
            model="demo",
            shell="shell",
            state="leased",
            validity="pending",
            created_at=MOMENT.isoformat(),
            updated_at=MOMENT.isoformat(),
        )
    )
    await store.append(make_event(EventType.TRIAL_CREATED, salt=0))
    report = await verify_projection(store, db, batch_id=BATCH)
    assert not report.ok
    assert report.mismatches[0].event_state == "pending"
    assert report.mismatches[0].db_state == "leased"
