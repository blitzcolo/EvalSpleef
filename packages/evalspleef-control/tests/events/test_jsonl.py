"""Tests for the JSONL event store (one append-only file per batch)."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalspleef.control.events import EventStoreClosed, JsonlEventStore
from evalspleef.core.events import EventEnvelope, EventType, new_event_id


def make_event(
    batch_id: str,
    event_type: EventType,
    *,
    trial_id: str | None = None,
    actor: str = "controller",
    payload: dict[str, object] | None = None,
    salt: int = 0,
) -> EventEnvelope:
    """Deterministic envelope factory (fixed clock and rng)."""
    moment = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    event_id = new_event_id(clock=lambda: moment, rng=random.Random(salt))
    return EventEnvelope(
        event_id=event_id,
        ts=moment.isoformat(),
        batch_id=batch_id,
        trial_id=trial_id,
        type=event_type,
        actor=actor,
        payload=payload or {},
    )


async def test_append_assigns_per_batch_sequences(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    first = make_event("2026-11-b1", EventType.TRIAL_CREATED, trial_id="web-001.m.s.e1.t01", salt=1)
    second = make_event("2026-11-b1", EventType.TRIAL_LEASED, trial_id="web-001.m.s.e1.t01", salt=2)
    other_batch = make_event("2026-11-b2", EventType.TRIAL_CREATED, salt=3)

    assert await store.append(first) == 1
    assert await store.append(second) == 2
    # Sequence numbering is scoped per batch.
    assert await store.append(other_batch) == 1


async def test_read_filters_by_trial_and_after_seq(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    batch = "2026-11-b1"
    trial_a = "web-001.m.s.e1.t01"
    trial_b = "web-001.m.s.e1.t02"
    events = [
        make_event(batch, EventType.TRIAL_CREATED, trial_id=trial_a, salt=1),
        make_event(batch, EventType.TRIAL_LEASED, trial_id=trial_a, salt=2),
        make_event(batch, EventType.TRIAL_CREATED, trial_id=trial_b, salt=3),
    ]
    for event in events:
        await store.append(event)

    assert await store.read(batch) == events
    assert await store.read(batch, after_seq=1) == events[1:]
    assert await store.read(batch, trial_id=trial_a) == events[:2]
    assert await store.read(batch, trial_id="missing") == []
    assert await store.read("no-such-batch") == []


async def test_sequence_numbers_recover_from_file(tmp_path: Path) -> None:
    """A new store instance over the same directory continues the numbering."""
    first_store = JsonlEventStore(tmp_path, fsync=False)
    event = make_event("2026-11-b1", EventType.TRIAL_CREATED, salt=1)
    assert await first_store.append(event) == 1
    assert await first_store.append(event) == 2
    await first_store.close()

    reopened = JsonlEventStore(tmp_path, fsync=False)
    assert await reopened.append(event) == 3
    assert await reopened.read("2026-11-b1") == [event, event, event]


async def test_append_writes_canonical_json_lines(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    event = make_event(
        "2026-11-b1",
        EventType.FEEDBACK_SENT,
        trial_id="web-001.m.s.e1.t01",
        payload={"round": 2, "level": "F1"},
        salt=7,
    )
    await store.append(event)

    lines = (tmp_path / "2026-11-b1.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines == [event.to_json_line()]


async def test_invalid_batch_id_rejected(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    bad = make_event("../escape", EventType.TRIAL_CREATED, salt=1)
    with pytest.raises(ValueError, match="batch_id"):
        await store.append(bad)


async def test_operations_after_close_raise(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    event = make_event("2026-11-b1", EventType.TRIAL_CREATED, salt=1)
    await store.append(event)
    await store.close()
    with pytest.raises(EventStoreClosed):
        await store.append(event)
    with pytest.raises(EventStoreClosed):
        await store.read("2026-11-b1")


async def test_subscribe_yields_appends_and_ends_when_idle(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    batch = "2026-11-b1"
    events = [
        make_event(batch, EventType.TRIAL_CREATED, trial_id="web-001.m.s.e1.t01", salt=i)
        for i in range(3)
    ]
    received: list[EventEnvelope] = []
    enough = asyncio.Event()

    async def consume() -> None:
        async for event in store.subscribe(batch, wait_s=5.0):
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


async def test_subscribe_bounded_wait_ends_iteration(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    seen: list[EventEnvelope] = []
    # Empty batch: the iterator ends after the bounded wait, no events lost.
    async for event in store.subscribe("empty", wait_s=0.05):
        seen.append(event)
    # Existing events are replayed from after_seq before the wait starts.
    pre = make_event("b", EventType.TRIAL_CREATED, salt=1)
    await store.append(pre)
    async for event in store.subscribe("b", wait_s=0.05):
        seen.append(event)
    assert seen == [pre]


async def test_close_ends_pending_subscription(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path, fsync=False)
    finished = asyncio.Event()

    async def consume() -> None:
        async for _ in store.subscribe("b", wait_s=60.0):
            pass
        finished.set()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the subscriber start its first read
    await store.close()
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    await task
