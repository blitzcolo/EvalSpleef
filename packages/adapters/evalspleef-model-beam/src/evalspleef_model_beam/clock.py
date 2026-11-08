"""The clock the provider receives by injection.

Structurally identical to the clock of ``evalspleef-testing``, redeclared here because an
adapter may import the SPI only. Every wait and every duration measurement goes through it.
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
