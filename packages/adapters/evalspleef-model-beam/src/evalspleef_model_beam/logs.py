"""Read one stub's container log from Beam's workspace log API, newest page first.

Measured behaviour of ``GET /api/v1/logs/{workspace}`` (read-only probes against the real API):
``limit`` is the row count (default 100, at most 1000); ``tail`` and ``lines`` are silently
ignored; the filters that work are ``stub_id``, ``container_id`` and ``app_id`` (a ``deployment_id``
is ignored and returns the workspace's latest logs, which would be other deployments' output);
a page holds the newest rows in ascending time order; cursors are ignored and ``end_time`` is the
honoured upper bound, so older rows are read by paging backwards on the oldest timestamp.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = ["MAX_PAGES", "PAGE_LIMIT", "PageFetcher", "StubLog", "read_stub_log"]

PAGE_LIMIT = 1000
MAX_PAGES = 200
PageFetcher = Callable[[Mapping[str, str | int]], Awaitable[list[dict[str, Any]]]]
"""``(query) -> entries`` for one ``GET /logs/{ws}`` request."""


@dataclass(frozen=True)
class StubLog:
    lines: tuple[str, ...]
    """Oldest first."""
    truncated: bool
    """True when paging stopped for the byte budget, the page cap or the progress guard."""
    dropped: int
    """Returned entries that belonged to another stub (the filter should make this zero)."""


def _time_key(value: object) -> tuple[int, int] | None:
    """``(epoch seconds, nanoseconds)`` of an RFC 3339 timestamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    head, _, fraction = value.rstrip("Z").partition(".")
    try:
        seconds = int(datetime.fromisoformat(head).replace(tzinfo=UTC).timestamp())
        return seconds, int(fraction.ljust(9, "0")[:9]) if fraction else 0
    except ValueError:
        return None


def _line(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('timestamp', '')} {str(entry.get('message', '')).rstrip()}".rstrip()


async def read_stub_log(fetch_page: PageFetcher, stub_id: str, max_bytes: int) -> StubLog:
    """Page backwards from now until the log runs out, the budget is spent or paging stalls."""
    kept: dict[tuple[object, object], Mapping[str, Any]] = {}
    size = 0
    dropped = 0
    truncated = False
    end_time: str | None = None
    oldest_seen: tuple[int, int] | None = None
    for _ in range(MAX_PAGES):
        query: dict[str, str | int] = {"stub_id": stub_id, "limit": PAGE_LIMIT}
        if end_time is not None:
            query["end_time"] = end_time
        page = await fetch_page(query)
        if not page:
            break
        for entry in page:
            if entry.get("stub_id") != stub_id:
                dropped += 1
                continue
            key = (entry.get("timestamp"), entry.get("seq_num"))
            if key not in kept:
                kept[key] = entry
                size += len(_line(entry).encode()) + 1
        stamps = [(at, entry) for entry in page if (at := _time_key(entry.get("timestamp")))]
        if not stamps:
            truncated = True
            break
        oldest, oldest_entry = min(stamps, key=lambda stamp: stamp[0])
        if oldest_seen is not None and oldest >= oldest_seen:
            truncated = True  # the API ignored end_time: the same rows would come back forever
            break
        oldest_seen, end_time = oldest, str(oldest_entry["timestamp"])
        if size >= max_bytes:
            truncated = True
            break
    else:
        truncated = True
    ordered = sorted(
        kept.values(),
        key=lambda e: (_time_key(e.get("timestamp")) or (0, 0), int(e.get("seq_num") or 0)),
    )
    return StubLog(tuple(_line(entry) for entry in ordered), truncated, dropped)
