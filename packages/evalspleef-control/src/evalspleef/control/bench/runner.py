"""Run a benchmark plan through the gateway and keep every raw sample.

The runner only collects: single requests at concurrency 1, then the closed-loop sweep,
stopping where :func:`evalspleef.core.perf.should_stop` says so. Every reported number is
recomputed from the raw record by :func:`evalspleef.core.perf.bench_result`, so the stored
result can always be checked against the stored samples.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from evalspleef.core.perf import LevelMetrics, RequestSample, level_metrics, should_stop
from evalspleef.spi.gateway import GatewayAdmin

from ..probes.client import GatewayClient
from ..probes.clock import Clock, iso_utc
from .plan import BenchPlan, Shape
from .prompts import GENERATOR, TOKENS_PER_LINE, bench_prompt

__all__ = [
    "BenchRunner",
    "MonotonicClock",
    "RawSample",
    "Sampler",
    "bench_id_for",
    "gateway_sampler",
    "raw_record",
    "raw_sha256",
]

Sampler = Callable[[Mapping[str, Any]], Awaitable[RequestSample]]
"""Sends one request body and returns its raw sample."""

LevelHook = Callable[[Shape, LevelMetrics], None]


class MonotonicClock:
    """``time.perf_counter`` with the probe clock's interface: a benchmark's only clock."""

    def now(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class RawSample:
    phase: str
    """``single`` or ``sweep``."""
    shape: Shape
    concurrency: int
    index: int
    """Prompt index; with the plan's seed it regenerates the exact prompt."""
    sample: RequestSample

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "input_target": self.shape.input_tokens,
            "output_target": self.shape.output_tokens,
            "concurrency": self.concurrency,
            "index": self.index,
            **self.sample.to_dict(),
        }


class BenchRunner:
    """Executes a :class:`BenchPlan` against one model name with an injected sampler."""

    def __init__(
        self,
        plan: BenchPlan,
        sampler: Sampler,
        *,
        model_name: str,
        on_level: LevelHook | None = None,
    ) -> None:
        self._plan = plan
        self._sampler = sampler
        self._model = model_name
        self._on_level = on_level

    def body(self, shape: Shape, index: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            **self._plan.extra_body,
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": bench_prompt(self._plan.seed, index, shape.input_tokens),
                }
            ],
            "max_tokens": shape.output_tokens,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._plan.ignore_eos:
            body["ignore_eos"] = True
        return body

    async def run(self) -> list[RawSample]:
        plan = self._plan
        samples: list[RawSample] = []
        index = 0
        for shape in plan.shapes:
            for _ in range(plan.single_repeats):
                sample = await self._sampler(self.body(shape, index))
                samples.append(RawSample("single", shape, 1, index, sample))
                index += 1
        for shape in plan.sweep:
            previous: LevelMetrics | None = None
            for concurrency in plan.concurrency:
                level, index = await self._level(shape, concurrency, index)
                samples.extend(level)
                metrics = level_metrics(
                    [item.sample for item in level], concurrency=concurrency, trim=plan.trim
                )
                if self._on_level is not None:
                    self._on_level(shape, metrics)
                if should_stop(previous, metrics, min_gain=plan.min_gain):
                    break
                previous = metrics
        return samples

    async def _level(
        self, shape: Shape, concurrency: int, first_index: int
    ) -> tuple[list[RawSample], int]:
        """``concurrency`` workers, each sending its requests back to back (closed loop)."""
        per_worker = self._plan.requests_per_worker

        async def worker(number: int) -> list[RawSample]:
            out: list[RawSample] = []
            for step in range(per_worker):
                # Indices are fixed before anything runs, so prompts never depend on timing.
                index = first_index + number * per_worker + step
                sample = await self._sampler(self.body(shape, index))
                out.append(RawSample("sweep", shape, concurrency, index, sample))
            return out

        results = await asyncio.gather(*(worker(number) for number in range(concurrency)))
        flat = sorted((item for batch in results for item in batch), key=lambda s: s.index)
        return flat, first_index + concurrency * per_worker


def bench_id_for(endpoint_id: str, created_at: str, seed: int) -> str:
    digest = hashlib.sha256(f"{endpoint_id}|{created_at}|{seed}".encode()).hexdigest()
    return f"bn_{digest[:16]}"


def raw_record(
    *,
    bench_id: str,
    endpoint_id: str,
    model_name: str,
    created_at: str,
    plan: BenchPlan,
    samples: list[RawSample],
    price_per_hour_usd: float | None,
    clock: str,
    gateway_trial_id: str | None = None,
) -> dict[str, Any]:
    """The stored raw benchmark: everything :func:`core.perf.bench_result` needs, and the
    inputs that regenerate every prompt (generator, seed, indices, lengths)."""
    return {
        "bench_id": bench_id,
        "endpoint_id": endpoint_id,
        "model_name": model_name,
        "created_at": created_at,
        "clock": clock,
        "gateway_trial_id": gateway_trial_id,
        "prompt_generator": {"name": GENERATOR, "tokens_per_line": TOKENS_PER_LINE},
        "plan": plan.to_dict(),
        "trim": plan.trim,
        "min_gain": plan.min_gain,
        "price_per_hour_usd": price_per_hour_usd,
        "samples": [sample.to_dict() for sample in samples],
    }


def raw_sha256(raw: Mapping[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@asynccontextmanager
async def gateway_sampler(
    admin: GatewayAdmin,
    *,
    gateway_url: str,
    endpoint_id: str,
    trial_id: str,
    plan: BenchPlan,
    budget_usd: float,
    clock: Clock,
    wall_clock: Clock,
    connect_timeout_s: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[Sampler]:
    """A sampler that streams through the gateway data plane with a short-lived bench key.

    ``clock`` stamps the samples (monotonic); ``wall_clock`` dates the key. The gateway
    files its own usage records (with its timing of every request) under ``trial_id``. The
    key is revoked when the block exits.
    """
    now = wall_clock.now()
    key = await admin.issue_key(
        trial_id=trial_id,
        endpoint_id=endpoint_id,
        max_budget_usd=budget_usd,
        max_tokens=plan.token_budget(),
        expires_at=iso_utc(now + plan.request_timeout_s * plan.request_count() + 3600),
    )
    try:
        async with GatewayClient(
            gateway_url,
            key.secret,
            clock=clock,
            request_timeout_s=plan.request_timeout_s,
            connect_timeout_s=connect_timeout_s,
            transport=transport,
        ) as client:

            async def sample(body: Mapping[str, Any]) -> RequestSample:
                return await client.stream_sample(body)

            yield sample
    finally:
        await admin.revoke_key(key.key_id)
