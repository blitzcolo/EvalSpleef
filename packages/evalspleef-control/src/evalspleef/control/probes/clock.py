"""Injectable wall clock shared by the probe runner and the endpoint manager."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

__all__ = ["Clock", "SystemClock", "iso_utc"]


class Clock(Protocol):
    """Epoch-seconds clock with an awaitable sleep, so tests can drive time by hand."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def iso_utc(epoch: float) -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix, the format stored in rows and events."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
