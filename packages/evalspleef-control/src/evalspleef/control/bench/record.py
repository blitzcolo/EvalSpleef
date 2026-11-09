"""Persist a benchmark: raw samples and computed result side by side, plus the store row."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalspleef.core.perf import bench_result
from evalspleef.spi.model import ModelSpec

from ..store.repos import BenchmarkRepository
from ..store.rows import BenchmarkRow
from .runner import raw_sha256

__all__ = ["API_NOTE", "SELF_SERVED_NOTE", "SavedBench", "hourly_price", "result_document", "save"]

API_NOTE = "hosted API: includes the network path and provider-side queueing"
SELF_SERVED_NOTE = (
    "through the gateway: includes the gateway hop and the network path to the engine"
)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def hourly_price(spec: ModelSpec) -> float | None:
    """The endpoint's declared hourly price: ``price_per_hour_usd``, or the per-GPU price
    times ``gpu_count`` (1 when unset). ``None`` for per-token APIs."""
    params = spec.deployment.params
    explicit = _number(params.get("price_per_hour_usd"))
    if explicit is not None:
        return explicit
    per_gpu = _number(params.get("price_per_gpu_hour_usd"))
    if per_gpu is None:
        return None
    count = params.get("gpu_count")
    return per_gpu * (count if isinstance(count, int) and count > 0 else 1)


def result_document(
    raw: Mapping[str, Any], *, facts: Mapping[str, Any], raw_file: str
) -> dict[str, Any]:
    """The stored result: identity, facts and :func:`bench_result` of ``raw``."""
    return {
        "bench_id": raw["bench_id"],
        "endpoint_id": raw["endpoint_id"],
        "model_name": raw["model_name"],
        "created_at": raw["created_at"],
        "plan": raw["plan"],
        "raw_file": raw_file,
        "raw_sha256": raw_sha256(raw),
        "facts": dict(facts),
        **bench_result(raw),
    }


@dataclass(frozen=True)
class SavedBench:
    row: BenchmarkRow
    raw_path: Path
    result_path: Path


async def save(
    raw: Mapping[str, Any],
    *,
    facts: Mapping[str, Any],
    directory: Path,
    repository: BenchmarkRepository,
) -> SavedBench:
    """Write ``raw.json`` and ``result.json`` under ``directory/<bench_id>/`` and insert the row."""
    folder = directory / str(raw["bench_id"])
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / "raw.json"
    result_path = folder / "result.json"
    raw_path.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    document = result_document(raw, facts=facts, raw_file=raw_path.name)
    result_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    row = BenchmarkRow(
        bench_id=str(raw["bench_id"]),
        endpoint_id=str(raw["endpoint_id"]),
        created_at=str(raw["created_at"]),
        plan=dict(raw["plan"]),
        facts=dict(facts),
        result=document,
        raw_path=str(raw_path),
        raw_sha256=str(document["raw_sha256"]),
    )
    await repository.insert(row)
    return SavedBench(row=row, raw_path=raw_path, result_path=result_path)
