"""SQL event store: events in a relational table, portable across backends.

Uses the :class:`~evalspleef.control.store.db.Database` abstraction, so the same
code runs on SQLite (thread executor) and PostgreSQL (psycopg 3). The envelope
is stored as its canonical JSON line; ``batch_id``/``seq`` form the primary key
and ``trial_id``/``type`` are indexed projections for filtered reads.

Cross-process following works by polling: local appends wake waiters
immediately through an in-process event, while writers in other processes are
picked up every ``poll_s`` seconds. Both paths stay bounded by ``wait_s``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from evalspleef.core.events import EventEnvelope

from ..store.db import Database
from .base import EventStore, EventStoreClosed, follow_events

__all__ = ["SqlEventStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    trial_id TEXT,
    type TEXT NOT NULL,
    envelope TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
)
"""

_INSERT = (
    "INSERT INTO event_log (batch_id, seq, event_id, trial_id, type, envelope) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)

_MAX_SEQ = "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM event_log WHERE batch_id = %s"

_READ = "SELECT envelope FROM event_log WHERE batch_id = %s AND seq > %s {trial} ORDER BY seq"


class SqlEventStore(EventStore):
    """Append-only event log backed by a relational table."""

    def __init__(self, db: Database, *, poll_s: float = 0.5) -> None:
        self._db = db
        self._poll_s = poll_s
        self._lock = asyncio.Lock()
        self._notify = asyncio.Event()
        self._closed = False
        self._ensured = False

    async def ensure_schema(self) -> None:
        """Create the ``event_log`` table when absent; idempotent."""
        async with self._lock:
            if not self._ensured:
                await self._db.execute(_SCHEMA)
                self._ensured = True

    async def append(self, event: EventEnvelope) -> int:
        async with self._lock:
            if self._closed:
                raise EventStoreClosed("sql event store is closed")
            if not self._ensured:
                await self._db.execute(_SCHEMA)
                self._ensured = True
            async with self._db.transaction():
                row = await self._db.fetch_one(_MAX_SEQ, (event.batch_id,))
                seq = int(row["max_seq"]) + 1 if row else 1
                await self._db.execute(
                    _INSERT,
                    (
                        event.batch_id,
                        seq,
                        event.event_id,
                        event.trial_id,
                        str(event.type),
                        event.to_json_line(),
                    ),
                )
            self._notify.set()
            return seq

    async def read(
        self,
        batch_id: str,
        *,
        trial_id: str | None = None,
        after_seq: int = 0,
    ) -> list[EventEnvelope]:
        if not self._ensured:
            await self.ensure_schema()
        if trial_id is None:
            sql = _READ.format(trial="")
            params: tuple[object, ...] = (batch_id, after_seq)
        else:
            sql = _READ.format(trial="AND trial_id = %s")
            params = (batch_id, after_seq, trial_id)
        rows = await self._db.fetch(sql, params)
        return [EventEnvelope.from_json_line(str(row["envelope"])) for row in rows]

    def subscribe(
        self,
        batch_id: str,
        *,
        trial_id: str | None = None,
        after_seq: int = 0,
        wait_s: float = 30.0,
    ) -> AsyncIterator[EventEnvelope]:
        async def wait(timeout_s: float) -> bool:
            self._notify.clear()
            try:
                await asyncio.wait_for(self._notify.wait(), timeout=timeout_s)
            except TimeoutError:
                return False
            return True

        async def read(cursor: int) -> list[EventEnvelope]:
            return await self.read(batch_id, trial_id=trial_id, after_seq=cursor)

        return follow_events(read, wait, after_seq=after_seq, wait_s=wait_s, poll_s=self._poll_s)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._notify.set()
