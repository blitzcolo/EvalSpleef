from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock: time moves only through ``advance``.

    ``sleep`` suspends until the clock has been advanced to or past the deadline, so tests
    never wait on wall time.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    @property
    def pending_sleepers(self) -> int:
        return len(self._sleepers)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot move backwards")
        self._now += seconds
        due = [entry for entry in self._sleepers if entry[0] <= self._now]
        self._sleepers = [entry for entry in self._sleepers if entry[0] > self._now]
        for _, future in due:
            if not future.done():
                future.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self._now + seconds, future)
        self._sleepers.append(entry)
        try:
            await future
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)
