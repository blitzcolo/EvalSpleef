"""Event store interface and shared tailing logic.

The event log is append-only and authoritative; every state change must be
accompanied by an event, and database rows are only projections of the log
(AGENTS.md architecture rule 6, PRD appendix B.8).

Sequence numbers are scoped per ``batch_id``: they start at 1, are assigned by
the store in ``append``, and ``read(batch_id, after_seq=n)`` returns only
events with a strictly greater sequence so callers can resume incrementally.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable

from evalspleef.core.events import EventEnvelope

__all__ = [
    "EventStore",
    "EventStoreClosed",
    "EventStoreError",
    "follow_events",
]


class EventStoreError(Exception):
    """Base class for event store failures."""


class EventStoreClosed(EventStoreError):
    """Raised when an operation is attempted on a closed store."""


class EventStore(ABC):
    """Append-only, authoritative event log (PRD appendix B.8).

    Implementations: :class:`evalspleef.control.events.JsonlEventStore` (one
    file per batch) and :class:`evalspleef.control.events.SqlEventStore`
    (relational table, see :mod:`evalspleef.control.store`).
    """

    @abstractmethod
    async def append(self, event: EventEnvelope) -> int:
        """Append ``event`` and return its assigned per-batch sequence number."""

    @abstractmethod
    async def read(
        self,
        batch_id: str,
        *,
        trial_id: str | None = None,
        after_seq: int = 0,
    ) -> list[EventEnvelope]:
        """Return events of ``batch_id`` with sequence greater than ``after_seq``.

        Events are returned in ascending sequence order. ``trial_id`` filters to
        one trial; ``None`` returns the whole batch.
        """

    @abstractmethod
    def subscribe(
        self,
        batch_id: str,
        *,
        trial_id: str | None = None,
        after_seq: int = 0,
        wait_s: float = 30.0,
    ) -> AsyncIterator[EventEnvelope]:
        """Iterate stored events, following new appends.

        Yields events with sequence greater than ``after_seq`` as they appear.
        The wait for the next event is bounded: when no new event arrives within
        ``wait_s`` seconds of the last one, the iterator ends. This is a
        drain-and-follow primitive, not a durable subscription: a consumer that
        needs every event records the sequence of the last yielded event and
        resubscribes from there.
        """

    @abstractmethod
    async def close(self) -> None:
        """Flush and release resources; pending subscribers end their iteration."""


#: Reads events after ``cursor``; used by :func:`follow_events`.
_ReadFn = Callable[[int], Awaitable[list[EventEnvelope]]]
#: Waits up to ``timeout`` seconds for a notification; returns False on timeout.
_WaitFn = Callable[[float], Awaitable[bool]]


async def follow_events(
    read: _ReadFn,
    wait: _WaitFn,
    *,
    after_seq: int = 0,
    wait_s: float = 30.0,
    poll_s: float | None = None,
) -> AsyncIterator[EventEnvelope]:
    """Shared bounded-wait tailing loop used by the concrete stores.

    ``read`` returns events after the cursor; ``wait`` blocks for a store
    notification and reports whether one arrived. ``poll_s`` bounds each wait
    for stores that must also observe writers in other processes (SQL); the
    loop re-reads after every poll tick. Without ``poll_s`` a single timeout
    already spans the whole remaining ``wait_s`` budget.
    """
    cursor = after_seq
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        try:
            events = await read(cursor)
        except EventStoreClosed:
            return
        if events:
            for event in events:
                cursor += 1
                yield event
            deadline = loop.time() + wait_s
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        step = remaining if poll_s is None else min(remaining, poll_s)
        notified = await wait(step)
        if not notified and poll_s is None:
            # The bounded wait elapsed without a new event: end the iteration.
            return
