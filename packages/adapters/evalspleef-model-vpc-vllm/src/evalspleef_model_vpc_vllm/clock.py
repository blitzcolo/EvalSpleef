"""Clock protocol and implementations for deterministic timing.

Every wait and time measurement in the provider goes through an injected clock.
"""

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


class SteppingClock:
    """Deterministic clock whose sleep advances time without real-world waiting."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot move backwards")
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
