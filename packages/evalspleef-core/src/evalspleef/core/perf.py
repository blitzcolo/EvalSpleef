"""Inference performance metrics from raw per-request samples.

Definitions follow PLAN.md "Design: inference performance metrics in every report". Every
number here is a pure function of the stored raw samples (no clock, no randomness), so a
benchmark or a batch can be recomputed from its raw record the way a grade is regraded.

Units: times in seconds on one monotonic clock per sample set, throughput in tokens per
second. A request that was not streamed has no TTFT, ITL or TPOT (``None``), never an
estimate.

A generated-text chunk is a streamed chunk whose delta has non-empty ``content``, non-empty
``reasoning_content``, or a ``tool_calls`` entry with a non-empty ``function.name`` or
``function.arguments``; role-only, empty and usage-only chunks are not. Whoever records the
samples (the gateway, the benchmark client) applies that rule; this module only reads the
recorded times.

Percentiles interpolate linearly between closest ranks (the convention of the client-side
check scripts behind ``docs/evidence/beam-1m-kv.md``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

__all__ = [
    "MIN_GAIN",
    "TRIM_FRACTION",
    "Distribution",
    "LevelMetrics",
    "ObservedRequest",
    "RequestMetrics",
    "RequestSample",
    "SaturationDecision",
    "SetMetrics",
    "Window",
    "bench_result",
    "busy_seconds",
    "distribution",
    "level_metrics",
    "observed_metrics",
    "percentile",
    "request_metrics",
    "saturation",
    "set_metrics",
    "should_stop",
    "steady_window",
    "usd_per_million_output_tokens",
]

TRIM_FRACTION = 0.10
"""Share of the run dropped at each end to get the steady window."""

MIN_GAIN = 0.05
"""A concurrency step must add at least this share of system decode throughput."""

_MTOK_PER_HOUR_PER_TOK_S = 0.0036
"""tok/s * 3600 s / 1e6 = million tokens per hour."""


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks; ``q`` in [0, 100]; ``None`` when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100.0
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass(frozen=True)
class Distribution:
    n: int
    mean: float | None
    p50: float | None
    p95: float | None
    p99: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": _round(self.mean),
            "p50": _round(self.p50),
            "p95": _round(self.p95),
            "p99": _round(self.p99),
        }


def distribution(values: Iterable[float | None]) -> Distribution:
    present = [value for value in values if value is not None]
    if not present:
        return Distribution(0, None, None, None, None)
    return Distribution(
        n=len(present),
        mean=sum(present) / len(present),
        p50=percentile(present, 50),
        p95=percentile(present, 95),
        p99=percentile(present, 99),
    )


def _round(value: float | None, digits: int = 9) -> float | None:
    return None if value is None else round(value, digits)


# -- one request ------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestSample:
    """What a client (or the gateway) observed for one request; the raw record."""

    t_start: float
    """Request sent (client) or received (gateway)."""
    t_end: float
    """Last chunk, or the whole body when not streamed."""
    chunk_times: tuple[float, ...] = ()
    """Arrival time of every chunk that carried generated text (content, reasoning or a
    tool call's name or arguments)."""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    """From the usage record; reasoning tokens included."""
    ok: bool = True
    streamed: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "t_start": self.t_start,
            "t_end": self.t_end,
            "chunk_times": list(self.chunk_times),
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "ok": self.ok,
            "streamed": self.streamed,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RequestSample:
        error = raw.get("error")
        return cls(
            t_start=float(raw["t_start"]),
            t_end=float(raw["t_end"]),
            chunk_times=tuple(float(value) for value in raw.get("chunk_times") or ()),
            input_tokens=int(raw.get("input_tokens") or 0),
            cached_tokens=int(raw.get("cached_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            ok=bool(raw.get("ok", True)),
            streamed=bool(raw.get("streamed", True)),
            error=str(error) if error is not None else None,
        )


@dataclass(frozen=True)
class RequestMetrics:
    ttft_s: float | None
    itl_s: tuple[float, ...]
    tpot_s: float | None
    e2e_s: float
    prefill_tok_s: float | None
    """Uncached input tokens over TTFT: cached tokens are not prefill work."""
    decode_tok_s: float | None


def request_metrics(sample: RequestSample) -> RequestMetrics:
    e2e = sample.t_end - sample.t_start
    chunks = sample.chunk_times
    if not sample.streamed or not chunks:
        return RequestMetrics(None, (), None, e2e, None, None)
    ttft = chunks[0] - sample.t_start
    gaps = tuple(later - earlier for earlier, later in pairwise(chunks))
    tpot = (
        (chunks[-1] - chunks[0]) / (sample.output_tokens - 1)
        if sample.output_tokens > 1 and len(chunks) > 1
        else None
    )
    uncached = max(sample.input_tokens - sample.cached_tokens, 0)
    return RequestMetrics(
        ttft_s=ttft,
        itl_s=gaps,
        tpot_s=tpot,
        e2e_s=e2e,
        prefill_tok_s=uncached / ttft if ttft > 0 else None,
        decode_tok_s=1.0 / tpot if tpot else None,
    )


# -- a set of requests ------------------------------------------------------------------


@dataclass(frozen=True)
class SetMetrics:
    requests: int
    ok: int
    failed: int
    with_ttft: int
    """Successful requests that were streamed and produced generated text."""
    ttft_s: Distribution
    itl_s: Distribution
    """Over every gap of every successful request."""
    tpot_s: Distribution
    e2e_s: Distribution
    prefill_tok_s: Distribution
    decode_tok_s: Distribution
    input_tokens: Distribution
    output_tokens: Distribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "ok": self.ok,
            "failed": self.failed,
            "with_ttft": self.with_ttft,
            "ttft_s": self.ttft_s.to_dict(),
            "itl_s": self.itl_s.to_dict(),
            "tpot_s": self.tpot_s.to_dict(),
            "e2e_s": self.e2e_s.to_dict(),
            "prefill_tok_s": self.prefill_tok_s.to_dict(),
            "decode_tok_s": self.decode_tok_s.to_dict(),
            "input_tokens": self.input_tokens.to_dict(),
            "output_tokens": self.output_tokens.to_dict(),
        }


def set_metrics(samples: Sequence[RequestSample]) -> SetMetrics:
    good = [sample for sample in samples if sample.ok]
    metrics = [request_metrics(sample) for sample in good]
    return SetMetrics(
        requests=len(samples),
        ok=len(good),
        failed=len(samples) - len(good),
        with_ttft=sum(1 for m in metrics if m.ttft_s is not None),
        ttft_s=distribution(m.ttft_s for m in metrics),
        itl_s=distribution(gap for m in metrics for gap in m.itl_s),
        tpot_s=distribution(m.tpot_s for m in metrics),
        e2e_s=distribution(m.e2e_s for m in metrics),
        prefill_tok_s=distribution(m.prefill_tok_s for m in metrics),
        decode_tok_s=distribution(m.decode_tok_s for m in metrics),
        input_tokens=distribution(float(s.input_tokens) for s in good),
        output_tokens=distribution(float(s.output_tokens) for s in good),
    )


# -- one concurrency level --------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def steady_window(samples: Sequence[RequestSample], trim: float = TRIM_FRACTION) -> Window | None:
    """The run (first send to last end) minus ``trim`` of its length at each end."""
    if not samples:
        return None
    first = min(sample.t_start for sample in samples)
    last = max(sample.t_end for sample in samples)
    span = last - first
    return Window(first + trim * span, last - trim * span)


@dataclass(frozen=True)
class LevelMetrics:
    """One closed-loop run at a fixed concurrency."""

    concurrency: int
    requests: int
    ok: int
    failed: int
    run_s: float
    window: Window | None
    in_window: int
    """Successful requests that ended inside the steady window."""
    system_prefill_tok_s: float | None
    """Uncached input tokens of the in-window requests over the window length."""
    system_decode_tok_s: float | None
    """Output tokens of the in-window requests over the window length."""
    metrics: SetMetrics
    """Per-request metrics of the in-window requests (the full-load figures at saturation)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "requests": self.requests,
            "ok": self.ok,
            "failed": self.failed,
            "run_s": _round(self.run_s),
            "window_s": _round(self.window.length) if self.window else None,
            "in_window": self.in_window,
            "system_prefill_tok_s": _round(self.system_prefill_tok_s),
            "system_decode_tok_s": _round(self.system_decode_tok_s),
            "metrics": self.metrics.to_dict(),
        }


def level_metrics(
    samples: Sequence[RequestSample], *, concurrency: int, trim: float = TRIM_FRACTION
) -> LevelMetrics:
    window = steady_window(samples, trim)
    run_s = (window.length / (1 - 2 * trim)) if window is not None and trim < 0.5 else 0.0
    good = [sample for sample in samples if sample.ok]
    inside = (
        [s for s in good if window.start <= s.t_end <= window.end] if window is not None else []
    )
    length = window.length if window is not None else 0.0
    prefill = decode = None
    if length > 0:
        prefill = sum(max(s.input_tokens - s.cached_tokens, 0) for s in inside) / length
        decode = sum(s.output_tokens for s in inside) / length
    return LevelMetrics(
        concurrency=concurrency,
        requests=len(samples),
        ok=len(good),
        failed=len(samples) - len(good),
        run_s=run_s,
        window=window,
        in_window=len(inside),
        system_prefill_tok_s=prefill,
        system_decode_tok_s=decode,
        metrics=set_metrics(inside),
    )


# -- the saturation sweep ---------------------------------------------------------------


def _decode(level: LevelMetrics) -> float:
    return level.system_decode_tok_s or 0.0


def should_stop(
    previous: LevelMetrics | None, current: LevelMetrics, *, min_gain: float = MIN_GAIN
) -> bool:
    """Stop raising concurrency after ``current``: it failed, or gained less than ``min_gain``."""
    if current.failed > 0:
        return True
    return previous is not None and _decode(current) < _decode(previous) * (1 + min_gain)


@dataclass(frozen=True)
class SaturationDecision:
    concurrency: int | None
    """The saturated level; ``None`` when even the first level failed."""
    saturated: bool
    """False when the plan ran out of levels before throughput stopped rising."""
    reason: str
    """``gain_below_threshold``, ``next_level_failed``, ``first_level_failed`` or
    ``plan_exhausted``."""
    next_gain: float | None = None
    """Relative decode gain of the step after the saturated level."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "saturated": self.saturated,
            "reason": self.reason,
            "next_gain": _round(self.next_gain),
        }


def saturation(levels: Sequence[LevelMetrics], *, min_gain: float = MIN_GAIN) -> SaturationDecision:
    """The level where system decode throughput stops rising (PLAN.md definition).

    Concurrency is raised step by step; the saturated level is the last one before a step
    that fails or gains less than ``min_gain``.
    """
    if not levels:
        return SaturationDecision(None, False, "plan_exhausted")
    if levels[0].failed > 0:
        return SaturationDecision(None, False, "first_level_failed")
    for current, following in pairwise(levels):
        base = _decode(current)
        gain = (_decode(following) - base) / base if base > 0 else None
        if following.failed > 0:
            return SaturationDecision(current.concurrency, True, "next_level_failed", gain)
        if _decode(following) < base * (1 + min_gain):
            return SaturationDecision(current.concurrency, True, "gain_below_threshold", gain)
    return SaturationDecision(levels[-1].concurrency, False, "plan_exhausted")


def usd_per_million_output_tokens(
    price_per_hour_usd: float | None, system_decode_tok_s: float | None
) -> float | None:
    """Hourly price / (system decode tok/s * 0.0036)."""
    if price_per_hour_usd is None or not system_decode_tok_s:
        return None
    return price_per_hour_usd / (system_decode_tok_s * _MTOK_PER_HOUR_PER_TOK_S)


# -- a whole benchmark record -----------------------------------------------------------


def bench_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a benchmark's metrics from its raw record.

    ``raw`` holds ``samples`` (each a :class:`RequestSample` dict plus ``phase`` -
    ``single`` or ``sweep`` -, ``input_target``, ``output_target`` and ``concurrency``),
    and optionally ``trim``, ``min_gain`` and ``price_per_hour_usd``.
    """
    trim = float(raw.get("trim", TRIM_FRACTION))
    min_gain = float(raw.get("min_gain", MIN_GAIN))
    price = raw.get("price_per_hour_usd")
    price_per_hour = float(price) if isinstance(price, int | float) else None
    single: dict[tuple[int, int], list[RequestSample]] = {}
    sweep: dict[tuple[int, int], dict[int, list[RequestSample]]] = {}
    for item in raw.get("samples") or ():
        shape = (int(item["input_target"]), int(item["output_target"]))
        sample = RequestSample.from_dict(item)
        if item.get("phase") == "single":
            single.setdefault(shape, []).append(sample)
        else:
            by_concurrency = sweep.setdefault(shape, {})
            by_concurrency.setdefault(int(item["concurrency"]), []).append(sample)
    sweeps: list[dict[str, Any]] = []
    for shape, by_level in sorted(sweep.items()):
        levels = [
            level_metrics(samples, concurrency=concurrency, trim=trim)
            for concurrency, samples in sorted(by_level.items())
        ]
        decision = saturation(levels, min_gain=min_gain)
        full = next((lv for lv in levels if lv.concurrency == decision.concurrency), None)
        sweeps.append(
            {
                "input_target": shape[0],
                "output_target": shape[1],
                "levels": [level.to_dict() for level in levels],
                "saturation": decision.to_dict(),
                "full_load": full.to_dict() if full else None,
                "usd_per_million_output_tokens": _round(
                    usd_per_million_output_tokens(
                        price_per_hour, full.system_decode_tok_s if full else None
                    ),
                    4,
                ),
            }
        )
    return {
        "trim": trim,
        "min_gain": min_gain,
        "price_per_hour_usd": price_per_hour,
        "single": [
            {
                "input_target": shape[0],
                "output_target": shape[1],
                "metrics": set_metrics(samples).to_dict(),
            }
            for shape, samples in sorted(single.items())
        ],
        "sweep": sweeps,
    }


# -- passive per-request records (the gateway) ------------------------------------------


@dataclass(frozen=True)
class ObservedRequest:
    """One request as the gateway recorded it, in seconds; ITL only as aggregates."""

    t_end: float
    e2e_s: float | None
    ttft_s: float | None = None
    tpot_s: float | None = None
    itl_mean_s: float | None = None
    itl_p95_s: float | None = None
    itl_max_s: float | None = None
    itl_count: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    ok: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)


def busy_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    """Length of the union of ``(start, end)`` intervals."""
    total = 0.0
    current: tuple[float, float] | None = None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = (start, end)
        else:
            current = (current[0], max(current[1], end))
    if current is not None:
        total += current[1] - current[0]
    return total


def observed_metrics(requests: Sequence[ObservedRequest]) -> dict[str, Any]:
    """In-batch distributions; throughput over busy time is observed, not saturated."""
    good = [r for r in requests if r.ok]
    timed = [r for r in good if r.e2e_s is not None]
    busy = busy_seconds((r.t_end - (r.e2e_s or 0.0), r.t_end) for r in timed)
    gaps = sum(r.itl_count for r in good if r.itl_mean_s is not None)
    pooled = (
        sum((r.itl_mean_s or 0.0) * r.itl_count for r in good if r.itl_mean_s is not None) / gaps
        if gaps
        else None
    )
    maxima = [r.itl_max_s for r in good if r.itl_max_s is not None]
    return {
        "requests": len(requests),
        "ok": len(good),
        "with_ttft": sum(1 for r in good if r.ttft_s is not None),
        "with_e2e": len(timed),
        "ttft_s": distribution(r.ttft_s for r in good).to_dict(),
        "tpot_s": distribution(r.tpot_s for r in good).to_dict(),
        "e2e_s": distribution(r.e2e_s for r in good).to_dict(),
        "itl_mean_s": _round(pooled),
        "itl_gaps": gaps,
        "itl_request_p95_s": distribution(r.itl_p95_s for r in good).to_dict(),
        "itl_max_s": _round(max(maxima)) if maxima else None,
        "busy_s": _round(busy),
        "observed_prefill_tok_s": _round(
            sum(max(r.input_tokens - r.cached_tokens, 0) for r in timed) / busy if busy else None
        ),
        "observed_decode_tok_s": _round(
            sum(r.output_tokens for r in timed) / busy if busy else None
        ),
    }
