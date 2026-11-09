"""Passive per-request timing on the gateway clock (PLAN.md inference performance design).

The gateway is the one data-plane choke point, so every carrier and vendor is timed the same
way and no adapter needs code. Timing never changes what the client asked for: a request
that was not streamed gets an end-to-end time and nothing else.

Definitions (shared with ``evalspleef.core.perf``, which the gateway may not import):
TTFT runs from the moment the request reached the gateway to the first chunk carrying
generated text (``content`` or ``reasoning_content``); ITL is each gap between consecutive
generated-text chunks; TPOT is ``(t_last - t_first) / (output_tokens - 1)``; E2E ends with
the last chunk. Percentiles interpolate linearly between closest ranks, like core.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

__all__ = ["RequestTimer", "RequestTiming", "carries_generated_text"]

_TEXT_FIELDS = ("content", "reasoning_content")


def carries_generated_text(chunk: Mapping[str, Any]) -> bool:
    """True when a chat completion chunk delivers generated text in any choice."""
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        delta = choice.get("delta") if isinstance(choice, Mapping) else None
        if isinstance(delta, Mapping) and any(
            isinstance(delta.get(name), str) and delta.get(name) for name in _TEXT_FIELDS
        ):
            return True
    return False


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * 0.95
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


@dataclass(frozen=True)
class RequestTiming:
    """The timing fields of one usage record; all ``None`` when nothing was timed."""

    ttft_ms: float | None = None
    e2e_ms: float | None = None
    tpot_ms: float | None = None
    itl_mean_ms: float | None = None
    itl_p95_ms: float | None = None
    itl_max_ms: float | None = None
    itl_count: int | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


class RequestTimer:
    """Times one request from the moment it reached the gateway."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self.received_at = now()
        self._chunk_times: list[float] = []

    def observe(self, chunk: Mapping[str, Any], at: float | None = None) -> None:
        """Note a streamed chat chunk; ``at`` is its arrival time when it was buffered earlier."""
        if carries_generated_text(chunk):
            self._chunk_times.append(self._now() if at is None else at)

    def fields(self, output_tokens: int) -> RequestTiming:
        """The usage-record timing fields, measured now (the end of the request)."""
        e2e = _ms(self._now() - self.received_at)
        chunks = self._chunk_times
        if not chunks:
            return RequestTiming(e2e_ms=e2e)
        gaps = [later - earlier for earlier, later in pairwise(chunks)]
        tpot = (
            _ms((chunks[-1] - chunks[0]) / (output_tokens - 1))
            if output_tokens > 1 and len(chunks) > 1
            else None
        )
        return RequestTiming(
            ttft_ms=_ms(chunks[0] - self.received_at),
            e2e_ms=e2e,
            tpot_ms=tpot,
            itl_mean_ms=_ms(sum(gaps) / len(gaps)) if gaps else None,
            itl_p95_ms=_ms(_p95(gaps)) if gaps else None,
            itl_max_ms=_ms(max(gaps)) if gaps else None,
            itl_count=len(gaps),
        )
