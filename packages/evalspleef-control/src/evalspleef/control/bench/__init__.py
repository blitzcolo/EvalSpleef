"""Inference benchmarks through the gateway (``spleef models bench``, probe P-12).

Definitions: PLAN.md "Design: inference performance metrics in every report"; the metrics
themselves are pure functions in :mod:`evalspleef.core.perf`.
"""

from .plan import BenchPlan, Shape, parse_shape
from .prompts import bench_prompt
from .record import API_NOTE, SELF_SERVED_NOTE, SavedBench, hourly_price, result_document, save
from .runner import (
    BenchRunner,
    MonotonicClock,
    RawSample,
    Sampler,
    bench_id_for,
    gateway_sampler,
    raw_record,
    raw_sha256,
)

__all__ = [
    "API_NOTE",
    "SELF_SERVED_NOTE",
    "BenchPlan",
    "BenchRunner",
    "MonotonicClock",
    "RawSample",
    "Sampler",
    "SavedBench",
    "Shape",
    "bench_id_for",
    "bench_prompt",
    "gateway_sampler",
    "hourly_price",
    "parse_shape",
    "raw_record",
    "raw_sha256",
    "result_document",
    "save",
]
