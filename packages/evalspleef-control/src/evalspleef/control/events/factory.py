"""Open an event store from a URL.

``jsonl:///<dir>`` opens a :class:`JsonlEventStore` (one append-only file per
batch); ``sqlite:///<path>`` and ``postgresql://...`` open a
:class:`SqlEventStore` on the matching database. SQL stores get their schema
created on first use.
"""

from __future__ import annotations

from .base import EventStore
from .jsonl import JsonlEventStore
from .sql import SqlEventStore

__all__ = ["open_event_store"]


async def open_event_store(url: str) -> EventStore:
    """Create the event store implementation matching ``url``."""
    if url.startswith("jsonl://"):
        directory = url[len("jsonl://") :]
        return JsonlEventStore(directory)
    if url.startswith("sqlite:///") or url.startswith("postgresql://"):
        from ..store.db import connect

        store = SqlEventStore(await connect(url))
        await store.ensure_schema()
        return store
    raise ValueError(
        f"unsupported event store URL (expected jsonl://, sqlite:/// or postgresql://): {url!r}"
    )
