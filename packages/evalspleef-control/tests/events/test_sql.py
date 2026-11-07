"""Tests for the SQL event store (SQLite backend) and open_event_store dispatch."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from evalspleef.control.events import (
    EventStoreClosed,
    JsonlEventStore,
    SqlEventStore,
    open_event_store,
)
from evalspleef.control.store import connect
from evalspleef.control.store.db import Database
from evalspleef.core.events import EventEnvelope, EventType, new_event_id


def make_event(
    batch_id: str,
    event_type: EventType,
    *,
    trial_id: str | None = None,
    payload: dict[str, object] | None = None,
    salt: int = 0,
) -> EventEnvelope:
    moment = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    return EventEnvelope(
        event_id=new_event_id(clock=lambda: moment, rng=random.Random(salt)),
        ts=moment.isoformat(),
        batch_id=batch_id,
        trial_id=trial_id,
        type=event_type,
        actor="controller",
        payload=payload or {},
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    yield database
    await database.close()


async def test_append_and_read_roundtrip(db: Database) -> None:
    store = SqlEventStore(db)
    trial_a = "web-001.m.s.e1.t01"
    trial_b = "web-001.m.s.e1.t02"
    events = [
        make_event("2026-11-b1", EventType.TRIAL_CREATED, trial_id=trial_a, salt=1),
        make_event("2026-11-b1", EventType.TRIAL_LEASED, trial_id=trial_a, salt=2),
        make_event("2026-11-b1", EventType.TRIAL_CREATED, trial_id=trial_b, salt=3),
    ]
    other_batch = make_event("2026-11-b2", EventType.TRIAL_CREATED, trial_id=trial_b, salt=4)
    assert await store.append(events[0]) == 1
    assert await store.append(events[1]) == 2
    assert await store.append(events[2]) == 3
    assert await store.append(other_batch) == 1  # per-batch numbering

    assert await store.read("2026-11-b1") == events
    assert await store.read("2026-11-b1", after_seq=1) == events[1:]
    assert await store.read("2026-11-b1", trial_id=trial_a) == events[:2]
    assert await store.read("2026-11-b2") == [other_batch]
    assert await store.read("other") == []


async def test_sequence_numbers_recover_after_reopen(db: Database) -> None:
    """A fresh store over the same database continues the per-batch numbering."""
    first = SqlEventStore(db)
    event = make_event("b", EventType.TRIAL_CREATED, salt=1)
    assert await first.append(event) == 1
    assert await first.append(event) == 2
    await first.close()

    reopened = SqlEventStore(db)
    assert await reopened.append(event) == 3
    assert len(await reopened.read("b")) == 3


async def test_subscribe_yields_appends(db: Database) -> None:
    store = SqlEventStore(db)
    events = [make_event("b", EventType.AGENT_STEP, trial_id="t", salt=i) for i in range(3)]
    received: list[EventEnvelope] = []
    enough = asyncio.Event()

    async def consume() -> None:
        async for event in store.subscribe("b", wait_s=5.0):
            received.append(event)
            if len(received) == len(events):
                enough.set()

    task = asyncio.create_task(consume())
    for event in events:
        await store.append(event)
    await asyncio.wait_for(enough.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert received == events


async def test_subscribe_bounded_wait_ends_iteration(db: Database) -> None:
    store = SqlEventStore(db)
    seen: list[EventEnvelope] = []
    async for event in store.subscribe("empty", wait_s=0.05):
        seen.append(event)
    assert seen == []


async def test_operations_after_close_raise(db: Database) -> None:
    store = SqlEventStore(db)
    event = make_event("b", EventType.TRIAL_CREATED, salt=1)
    await store.append(event)
    await store.close()
    with pytest.raises(EventStoreClosed):
        await store.append(event)


async def test_open_event_store_jsonl(tmp_path: Path) -> None:
    store = await open_event_store(f"jsonl://{tmp_path}")
    assert isinstance(store, JsonlEventStore)
    event = make_event("b", EventType.TRIAL_CREATED, salt=1)
    assert await store.append(event) == 1


async def test_open_event_store_sqlite(tmp_path: Path) -> None:
    store = await open_event_store(f"sqlite:///{tmp_path / 'events.db'}")
    assert isinstance(store, SqlEventStore)
    event = make_event("b", EventType.TRIAL_CREATED, salt=1)
    assert await store.append(event) == 1
    assert await store.read("b") == [event]


async def test_open_event_store_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="unsupported event store URL"):
        await open_event_store("memory://nowhere")
