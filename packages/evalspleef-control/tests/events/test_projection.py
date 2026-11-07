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


def test_projection_equals_live_machine() -> None:
    """Replay the same transitions through the machine and through events."""
    machine = TrialMachine(TRIAL)
    events: list[EventEnvelope] = [make_event(EventType.TRIAL_CREATED, salt=0)]
    transitions = [
        (EventType.TRIAL_LEASED, "lease"),
        (EventType.TRIAL_STARTED, "start"),
        (EventType.AGENT_STOPPED, "stop"),
        (EventType.FEEDBACK_DECIDED, "needs_feedback"),
        (EventType.FEEDBACK_SENT, "deliver"),
        (EventType.AGENT_STOPPED, "stop"),
        (EventType.VERIFY_STARTED, "finish"),
        (EventType.TRIAL_SCORED, "score"),
    ]
    for salt, (event_type, transition) in enumerate(transitions, start=1):
        machine.apply(transition)
        events.append(make_event(event_type, salt=salt))
    # Events that do not drive the trial state machine are ignored.
    events.append(make_event(EventType.EVIDENCE_COLLECTED, payload={"path": "/tmp/x"}, salt=99))

    assert project_trial_state(events, trial_id=TRIAL) is machine.state
    assert machine.state.value == "scored"


def test_projection_uses_payload_transition_override() -> None:
    """Non-default transitions (e.g. infra failures) are carried in the payload."""
    machine = TrialMachine(TRIAL)
    machine.apply("lease")
    machine.apply("fail_infra")
    events = [
        make_event(EventType.TRIAL_CREATED, salt=0),
        make_event(EventType.TRIAL_LEASED, salt=1),
        make_event(EventType.INCIDENT_OPENED, payload={"transition": "fail_infra"}, salt=2),
    ]
    projected = project_trial_state(events, trial_id=TRIAL)
    assert projected is machine.state
    assert projected.value == "failed_infra"


def test_projection_of_scored_then_invalidated() -> None:
    machine = TrialMachine(TRIAL)
    for transition in ("lease", "start", "stop", "finish", "score"):
        machine.apply(transition)
    machine.apply("invalidate_post_hoc")
    events = [
        make_event(EventType.TRIAL_CREATED, salt=0),
        make_event(EventType.TRIAL_LEASED, salt=1),
        make_event(EventType.TRIAL_STARTED, salt=2),
        make_event(EventType.AGENT_STOPPED, salt=3),
        make_event(EventType.VERIFY_STARTED, salt=4),
        make_event(EventType.TRIAL_SCORED, salt=5),
        make_event(EventType.TRIAL_INVALIDATED, salt=6),
    ]
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
    for salt, event_type in enumerate(
        [
            EventType.TRIAL_CREATED,
            EventType.TRIAL_LEASED,
            EventType.TRIAL_STARTED,
            EventType.AGENT_STOPPED,
            EventType.VERIFY_STARTED,
            EventType.TRIAL_SCORED,
        ]
    ):
        await store.append(make_event(event_type, salt=salt))

    report = await verify_projection(store, db, batch_id=BATCH)
    assert report.ok
    assert report.checked == 1


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
