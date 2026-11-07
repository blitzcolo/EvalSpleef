"""JSONL event store: one append-only file per batch.

Each batch lives in ``<directory>/<batch_id>.jsonl``. The canonical envelope
line (``EventEnvelope.to_json_line``) is the only on-disk representation, so
sequence numbers are recoverable from the file alone: the sequence of an event
is its 1-based line number and the next assigned sequence is the line count.
Reopening a directory therefore continues numbering where the previous process
stopped.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from evalspleef.core.events import EventEnvelope

from .base import EventStore, EventStoreClosed, follow_events

__all__ = ["JsonlEventStore"]


def _check_batch_id(batch_id: str) -> None:
    if not batch_id or batch_id in {".", ".."} or "/" in batch_id or "\\" in batch_id:
        raise ValueError(f"invalid batch_id for a jsonl file name: {batch_id!r}")


class JsonlEventStore(EventStore):
    """Append-only JSONL event log with one file per batch.

    Args:
        directory: Directory holding the per-batch ``<batch_id>.jsonl`` files.
        fsync: When True (default) every append is flushed to the OS and
            fsynced before ``append`` returns, so a confirmed append survives a
            machine crash. Disable only where durability is handled elsewhere.
    """

    def __init__(self, directory: str | Path, *, fsync: bool = True) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = asyncio.Lock()
        self._line_counts: dict[str, int] = {}
        self._notify = asyncio.Event()
        self._closed = False

    @property
    def directory(self) -> Path:
        """Directory holding the per-batch files."""
        return self._directory

    def _path(self, batch_id: str) -> Path:
        _check_batch_id(batch_id)
        return self._directory / f"{batch_id}.jsonl"

    @staticmethod
    def _count_lines(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("rb") as handle:
            return sum(1 for line in handle if line.strip())

    def _write_line(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())

    async def append(self, event: EventEnvelope) -> int:
        path = self._path(event.batch_id)
        line = event.to_json_line() + "\n"
        async with self._lock:
            if self._closed:
                raise EventStoreClosed("jsonl event store is closed")
            count = self._line_counts.get(event.batch_id)
            if count is None:
                count = await asyncio.to_thread(self._count_lines, path)
            seq = count + 1
            await asyncio.to_thread(self._write_line, path, line)
            self._line_counts[event.batch_id] = seq
            self._notify.set()
            return seq

    async def read(
        self,
        batch_id: str,
        *,
        trial_id: str | None = None,
        after_seq: int = 0,
    ) -> list[EventEnvelope]:
        path = self._path(batch_id)
        async with self._lock:
            if self._closed:
                raise EventStoreClosed("jsonl event store is closed")
            if not path.exists():
                return []
            lines = await asyncio.to_thread(path.read_text, encoding="utf-8")
        events: list[EventEnvelope] = []
        for seq, raw in enumerate(lines.splitlines(), start=1):
            if seq <= after_seq or not raw.strip():
                continue
            event = EventEnvelope.from_json_line(raw)
            if trial_id is not None and event.trial_id != trial_id:
                continue
            events.append(event)
        return events

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

        return follow_events(read, wait, after_seq=after_seq, wait_s=wait_s)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._notify.set()
