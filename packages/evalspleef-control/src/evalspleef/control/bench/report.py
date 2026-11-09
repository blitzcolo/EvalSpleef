"""Markdown tables for a benchmark result and for in-batch request distributions.

Used by ``spleef models bench`` and by the "Inference performance" section of a batch
release, so both print the same columns. Seconds become s or ms here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["bench_markdown", "facts_line", "observed_markdown"]


def _dist(container: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = container.get(name)
    return value if isinstance(value, Mapping) else {}


def _fmt(value: Any, scale: float = 1.0, digits: int = 2) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "-"
    return f"{value * scale:,.{digits}f}"


def _triple(dist: Mapping[str, Any], scale: float, digits: int) -> str:
    return " / ".join(_fmt(dist.get(key), scale, digits) for key in ("p50", "p95", "p99"))


_FACT_KEYS = (
    ("serving", "serving"),
    ("model_revision", "revision"),
    ("vendor_model_id", "vendor model"),
    ("inference_engine", "engine"),
    ("gpu_type", "GPU"),
    ("gpu_count", "GPUs"),
    ("quantization", "quantization"),
    ("kv_pool_tokens", "KV pool tokens"),
    ("engine_extra_args", "engine flags"),
    ("deployment_adapter", "deployment"),
    ("gateway_version", "gateway"),
    ("price_per_hour_usd", "USD/h"),
)


def facts_line(facts: Mapping[str, Any]) -> str:
    parts = [
        f"{label} {facts[key]}" for key, label in _FACT_KEYS if facts.get(key) not in (None, "")
    ]
    return "; ".join(parts) if parts else "no hardware or version facts recorded"


def bench_markdown(result: Mapping[str, Any]) -> list[str]:
    """The single-request table, the sweep table and the saturation line of one benchmark."""
    lines: list[str] = []
    singles = result.get("single") or []
    if singles:
        lines += [
            "Single request (concurrency 1, unique prefixes):",
            "",
            "| Shape (in x out) | OK | TTFT p50 s | Prefill tok/s p50 | Decode tok/s p50 "
            "| TPOT p50 ms | ITL mean / p50 / p95 / p99 ms | E2E p50 s |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in singles:
            metrics = item.get("metrics") or {}
            itl = _dist(metrics, "itl_s")
            itl_text = " / ".join(
                _fmt(itl.get(key), 1000, 1) for key in ("mean", "p50", "p95", "p99")
            )
            lines.append(
                f"| {item.get('input_target')}x{item.get('output_target')} "
                f"| {metrics.get('ok')}/{metrics.get('requests')} "
                f"| {_fmt(_dist(metrics, 'ttft_s').get('p50'), 1, 3)} "
                f"| {_fmt(_dist(metrics, 'prefill_tok_s').get('p50'), 1, 0)} "
                f"| {_fmt(_dist(metrics, 'decode_tok_s').get('p50'), 1, 1)} "
                f"| {_fmt(_dist(metrics, 'tpot_s').get('p50'), 1000, 1)} "
                f"| {itl_text} "
                f"| {_fmt(_dist(metrics, 'e2e_s').get('p50'), 1, 2)} |"
            )
        lines.append("")
    for sweep in result.get("sweep") or []:
        lines += [
            f"Concurrency sweep ({sweep.get('input_target')}x{sweep.get('output_target')}, "
            "closed loop, steady window = run minus first and last 10%):",
            "",
            "| Concurrency | OK | Window s (requests) | System prefill tok/s | System decode tok/s "
            "| TTFT p50 / p95 / p99 s | TPOT p50 / p95 / p99 ms | ITL p50 / p95 / p99 ms "
            "| E2E p50 / p95 / p99 s |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for level in sweep.get("levels") or []:
            metrics = level.get("metrics") or {}
            lines.append(
                f"| {level.get('concurrency')} | {level.get('ok')}/{level.get('requests')} "
                f"| {_fmt(level.get('window_s'), 1, 1)} ({level.get('in_window')}) "
                f"| {_fmt(level.get('system_prefill_tok_s'), 1, 1)} "
                f"| {_fmt(level.get('system_decode_tok_s'), 1, 1)} "
                f"| {_triple(_dist(metrics, 'ttft_s'), 1, 3)} "
                f"| {_triple(_dist(metrics, 'tpot_s'), 1000, 1)} "
                f"| {_triple(_dist(metrics, 'itl_s'), 1000, 1)} "
                f"| {_triple(_dist(metrics, 'e2e_s'), 1, 2)} |"
            )
        decision = sweep.get("saturation") or {}
        full = sweep.get("full_load") or {}
        if decision.get("concurrency") is None:
            verdict = f"no saturated level ({decision.get('reason')})"
        elif decision.get("saturated"):
            gain = decision.get("next_gain")
            verdict = (
                f"saturation at concurrency {decision.get('concurrency')} "
                f"({decision.get('reason')}"
                + (f", next step {_fmt(gain, 100, 1)}%" if gain is not None else "")
                + ")"
            )
        else:
            verdict = (
                f"not saturated: throughput still rose at concurrency "
                f"{decision.get('concurrency')}, the highest level planned"
            )
        cost = sweep.get("usd_per_million_output_tokens")
        lines += [
            "",
            f"{verdict}; system decode {_fmt(full.get('system_decode_tok_s'), 1, 1)} tok/s, "
            f"system prefill {_fmt(full.get('system_prefill_tok_s'), 1, 1)} tok/s"
            + (
                f"; {_fmt(cost, 1, 2)} USD per million output tokens at "
                f"{_fmt(result.get('price_per_hour_usd'), 1, 2)} USD/h"
                if cost is not None
                else "; no hourly price declared, no cost per million output tokens"
            ),
            "",
        ]
    return lines


def observed_markdown(observed: Mapping[str, Any]) -> list[str]:
    """In-batch distributions from the gateway's per-request fields."""
    return [
        f"In batch: {observed.get('requests')} request(s), {observed.get('ok')} ok, "
        f"{observed.get('with_ttft')} with a TTFT (streamed), "
        f"{observed.get('with_e2e')} with an E2E time.",
        "",
        "| Metric | n | mean | p50 | p95 | p99 |",
        "| --- | --- | --- | --- | --- | --- |",
        *(
            f"| {label} | {dist.get('n')} | {_fmt(dist.get('mean'), scale, digits)} "
            f"| {_fmt(dist.get('p50'), scale, digits)} | {_fmt(dist.get('p95'), scale, digits)} "
            f"| {_fmt(dist.get('p99'), scale, digits)} |"
            for label, dist, scale, digits in (
                ("TTFT s", _dist(observed, "ttft_s"), 1, 3),
                ("TPOT ms", _dist(observed, "tpot_s"), 1000, 1),
                ("ITL per-request p95 ms", _dist(observed, "itl_request_p95_s"), 1000, 1),
                ("E2E s", _dist(observed, "e2e_s"), 1, 2),
            )
        ),
        "",
        f"ITL mean over {observed.get('itl_gaps')} gap(s): "
        f"{_fmt(observed.get('itl_mean_s'), 1000, 1)} ms, max "
        f"{_fmt(observed.get('itl_max_s'), 1000, 1)} ms. Throughput observed over "
        f"{_fmt(observed.get('busy_s'), 1, 1)} s of busy time (observed, not saturated): "
        f"prefill {_fmt(observed.get('observed_prefill_tok_s'), 1, 1)} tok/s, "
        f"decode {_fmt(observed.get('observed_decode_tok_s'), 1, 1)} tok/s.",
        "",
    ]
