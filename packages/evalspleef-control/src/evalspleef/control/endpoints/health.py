"""Health loop: a ``max_tokens=1`` request per referenced endpoint every 60 s.

Each request costs a little money, so endpoints are only pinged while a batch is Running
(TECH-DESIGN section 6.2). The idle-spin check runs on every tick regardless, because an idle
WARM endpoint bills without serving any traffic (PRD FR-MOD-06).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from evalspleef.control.probes.clock import Clock
from evalspleef.control.probes.runner import PingResult

from .manager import EndpointManager

__all__ = ["HealthLoop", "RunningBatches"]

RunningBatches = Callable[[], Awaitable[Mapping[str, Sequence[str]]]]
"""Maps the id of every Running batch to the endpoint ids it references."""


class HealthLoop:
    def __init__(
        self,
        manager: EndpointManager,
        running_batches: RunningBatches,
        *,
        clock: Clock,
        interval_s: float = 60.0,
    ) -> None:
        self._manager = manager
        self._running = running_batches
        self._clock = clock
        self._interval_s = interval_s

    async def tick(self) -> dict[str, PingResult]:
        """One round: ping the endpoints of Running batches, then check for idle spin."""
        pings: dict[str, PingResult] = {}
        for batch_id, endpoint_ids in (await self._running()).items():
            for endpoint_id in endpoint_ids:
                if endpoint_id in pings:
                    continue
                ping = await self._manager.health_check(endpoint_id, batch_id=batch_id)
                if ping is not None:
                    pings[endpoint_id] = ping
        await self._manager.check_idle()
        return pings

    async def run(self, stop: asyncio.Event) -> None:
        """Tick every ``interval_s`` of clock time until ``stop`` is set."""
        while not stop.is_set():
            await self.tick()
            sleeper = asyncio.ensure_future(self._clock.sleep(self._interval_s))
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                stopper.cancel()
                await asyncio.gather(sleeper, stopper, return_exceptions=True)
