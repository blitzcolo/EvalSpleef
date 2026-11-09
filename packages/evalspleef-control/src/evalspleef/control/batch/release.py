"""The batch release package (PRD FR-RPT-2, FR-RPT-4, FR-ISO-01, NFR-02).

A release is the leaderboard plus everything a reader needs to audit a cell:
a drill-down per cell (model source and revision, deployment, engine, sandbox
and isolation level, cost basis), the version manifest, the probe report
summary, the per-carrier isolation section, the judge consistency figures and
the cost reconciliation. ``build_release`` only arranges data the store, the
event log, the freeze record and the ledger already hold; the caller writes
the returned files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.perf import ObservedRequest, observed_metrics
from evalspleef.core.report import BatchReport
from evalspleef.spi.model import ModelSpec

from ..bench.record import API_NOTE, SELF_SERVED_NOTE
from ..bench.report import bench_markdown, facts_line, observed_markdown
from ..ledger import ReconcileReport
from ..store.rows import BenchmarkRow, GradeRow, ProbeReportRow, TrialRow

__all__ = ["ReleaseInputs", "build_release"]


@dataclass(frozen=True)
class ReleaseInputs:
    report: BatchReport
    trials: Sequence[TrialRow]
    grades: Mapping[str, Sequence[GradeRow]]
    events: Sequence[EventEnvelope]
    freeze: Mapping[str, Any] | None
    probe_reports: Mapping[str, ProbeReportRow]
    models: Mapping[str, ModelSpec]
    judges: Sequence[str]
    costs: ReconcileReport | None
    benchmarks: Mapping[str, BenchmarkRow] = field(default_factory=dict)
    """Endpoint id -> the benchmark the release reports (the latest stored)."""


def build_release(inputs: ReleaseInputs) -> dict[str, str]:
    """File name -> content of every release artifact."""
    perf = _perf(inputs)
    return {
        "leaderboard.md": inputs.report.to_markdown() + _perf_markdown(perf),
        "perf.json": _json(perf),
        "drilldown.json": _json(_drilldown(inputs)),
        "versions.json": _json(_versions(inputs)),
        "probes.json": _json(_probes(inputs)),
        "isolation.md": _isolation(inputs),
        "judges.json": _json(_judges(inputs)),
        "costs.json": _json(_costs(inputs)),
    }


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _profiles(inputs: ReleaseInputs) -> Mapping[str, Mapping[str, Any]]:
    profiles = (inputs.freeze or {}).get("isolation_profiles") or {}
    return profiles if isinstance(profiles, Mapping) else {}


def _versions_of(row: TrialRow) -> Mapping[str, Any]:
    versions = row.extensions.get("versions")
    return versions if isinstance(versions, Mapping) else {}


def _drilldown(inputs: ReleaseInputs) -> list[dict[str, Any]]:
    profiles = _profiles(inputs)
    cells: list[dict[str, Any]] = []
    for cell in inputs.report.build_cells():
        rows = [row for row in inputs.trials if row.cell == cell.cell_id]
        versions = next((_versions_of(row) for row in rows if _versions_of(row)), {})
        instance = versions.get("provider_instance")
        profile = profiles.get(str(instance), {}) if instance else {}
        spec = inputs.models.get(cell.model)
        revision = versions.get("hf_revision")
        cells.append(
            {
                "cell_id": cell.cell_id,
                "cell": cell.format(),
                "status": str(cell.status),
                "trials": [row.trial_id for row in rows],
                "model_source": versions.get("model_source"),
                "hf_repo": versions.get("hf_repo"),
                "hf_revision_short": str(revision)[:7] if revision else None,
                "deployment_adapter": versions.get("deployment_adapter"),
                "inference_engine": versions.get("inference_engine"),
                "sandbox_adapter": versions.get("sandbox_adapter"),
                "provider_instance": instance,
                "execution_environment": versions.get("execution_environment"),
                "declared_isolation": _level(profile.get("declared_isolation")),
                "verified_isolation": _level(profile.get("verified_isolation")),
                "cost_basis": spec.pricing_ref.split(":", 1)[0] if spec else None,
                "pricing_ref": spec.pricing_ref if spec else None,
            }
        )
    return cells


def _level(value: Any) -> str | None:
    return f"L{value}" if isinstance(value, int) else None


def _versions(inputs: ReleaseInputs) -> dict[str, Any]:
    # ``None`` when the batch was frozen before provider environments were recorded.
    environments = (inputs.freeze or {}).get("provider_environments")
    return {
        "freeze": dict(inputs.freeze or {}),
        "provider_environments": dict(environments) if isinstance(environments, Mapping) else None,
        "trials": {row.trial_id: dict(_versions_of(row)) for row in inputs.trials},
    }


def _probes(inputs: ReleaseInputs) -> dict[str, Any]:
    frozen = (inputs.freeze or {}).get("probe_report_ids") or {}
    summary: dict[str, Any] = {}
    for endpoint_id, report_id in sorted(frozen.items()):
        row = inputs.probe_reports.get(str(report_id))
        if row is None:
            summary[endpoint_id] = {"report_id": report_id, "missing": True}
            continue
        checks = row.results.get("checks") or []
        summary[endpoint_id] = {
            "report_id": report_id,
            "gateway_version": row.gateway_version,
            "passed": sum(1 for check in checks if check.get("passed")),
            "total": len(checks),
            "failed": sorted(str(c.get("probe_id")) for c in checks if not c.get("passed")),
            "capabilities": dict(row.capabilities),
        }
    return summary


def _isolation(inputs: ReleaseInputs) -> str:
    preflights: dict[str, list[Mapping[str, Any]]] = {}
    for event in inputs.events:
        if event.type is EventType.PREFLIGHT_RESULT and event.payload:
            instance = str(event.payload.get("provider_instance"))
            preflights.setdefault(instance, []).append(event.payload)
    lines = [f"# Isolation report {inputs.report.batch_id}", ""]
    for instance, profile in sorted(_profiles(inputs).items()):
        runs = preflights.get(instance, [])
        passed = sum(1 for run in runs if run.get("passed"))
        lines += [
            f"## {instance} ({profile.get('provider')})",
            "",
            f"- declared isolation: {_level(profile.get('declared_isolation'))}",
            f"- verified isolation: {_level(profile.get('verified_isolation')) or 'not verified'}",
            f"- preflights: {passed} passed of {len(runs)}",
            "",
            "| invariant | how | waived | reason |",
            "| --- | --- | --- | --- |",
        ]
        for key, entry in sorted((profile.get("invariants") or {}).items()):
            waived = "yes" if entry.get("waived") else "no"
            how = str(entry.get("how", "")).replace("|", "/")
            reason = str(entry.get("reason") or "").replace("|", "/")
            lines.append(f"| {key} | {how} | {waived} | {reason} |")
        waivers = sorted({w for run in runs for w in run.get("waived") or ()})
        if waivers:
            lines += ["", f"Waived at preflight: {', '.join(waivers)}"]
        lines.append("")
    return "\n".join(lines)


def _judges(inputs: ReleaseInputs) -> dict[str, Any]:
    per_trial: dict[str, Any] = {}
    for trial_id, grades in sorted(inputs.grades.items()):
        if not grades:
            continue
        latest = grades[-1].payload
        per_trial[trial_id] = {
            "consistency": latest.get("consistency"),
            "judge_panel": latest.get("judge_panel") or [],
            "decided_by": latest.get("decided_by"),
        }
    values = [
        float(item["consistency"]["value"])
        for item in per_trial.values()
        if isinstance(item.get("consistency"), Mapping)
        and item["consistency"].get("value") is not None
    ]
    return {
        "panel": list(inputs.judges),
        "trials_with_consistency": len(values),
        "min_consistency": min(values) if values else None,
        "trials": per_trial,
    }


def _costs(inputs: ReleaseInputs) -> dict[str, Any]:
    if inputs.costs is None:
        return {"reconciled": False, "kinds": []}
    return {
        "reconciled": inputs.costs.ok,
        "tolerance": inputs.costs.tolerance,
        "kinds": [
            {
                "kind": str(kind.kind),
                "ledger_usd": round(kind.ledger_usd, 6),
                "bill_usd": round(kind.bill_usd, 6),
                "relative_difference": kind.relative_difference,
                "ok": kind.ok,
            }
            for kind in inputs.costs.kinds
        ],
    }


# -- inference performance ------------------------------------------------------------------

_IN_BATCH_LABEL = "observed over the batch's busy windows, not saturated"


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) / 1000.0


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _observed(item: Mapping[str, Any]) -> ObservedRequest | None:
    """One ``agent.step`` gateway request in seconds; ``None`` without a usable timestamp."""
    try:
        t_end = datetime.fromisoformat(str(item.get("ts"))).timestamp()
    except ValueError:
        return None
    return ObservedRequest(
        t_end=t_end,
        e2e_s=_seconds(item.get("e2e_ms")),
        ttft_s=_seconds(item.get("ttft_ms")),
        tpot_s=_seconds(item.get("tpot_ms")),
        itl_mean_s=_seconds(item.get("itl_mean_ms")),
        itl_p95_s=_seconds(item.get("itl_p95_ms")),
        itl_max_s=_seconds(item.get("itl_max_ms")),
        itl_count=_count(item.get("itl_count")),
        input_tokens=_count(item.get("input_tokens")),
        cached_tokens=_count(item.get("cached_tokens")),
        output_tokens=_count(item.get("output_tokens")),
        ok=item.get("status_code") == 200,
    )


def _batch_requests(
    inputs: ReleaseInputs,
) -> tuple[dict[str, list[ObservedRequest]], dict[str, str]]:
    """Endpoint -> observed requests, and endpoint -> a trial that called it."""
    requests: dict[str, list[ObservedRequest]] = {}
    trial_of: dict[str, str] = {}
    for event in inputs.events:
        if event.type is not EventType.AGENT_STEP or not event.payload:
            continue
        for item in event.payload.get("gateway_requests") or ():
            if not isinstance(item, Mapping):
                continue
            endpoint = str(item.get("endpoint_id") or "")
            observed = _observed(item)
            if not endpoint or observed is None:
                continue
            requests.setdefault(endpoint, []).append(observed)
            if event.trial_id:
                trial_of.setdefault(endpoint, event.trial_id)
    return requests, trial_of


def _trial_facts(inputs: ReleaseInputs, trial_id: str | None) -> dict[str, Any]:
    row = next((r for r in inputs.trials if r.trial_id == trial_id), None)
    versions = _versions_of(row) if row is not None else {}
    vendor = versions.get("model_source") == "vendor"
    keys = ("model_source", "hf_repo", "hf_revision", "inference_engine", "gpu_type")
    facts: dict[str, Any] = {key: versions.get(key) for key in keys if versions.get(key)}
    facts.update(
        {
            "model_revision": versions.get("hf_revision"),
            "deployment_adapter": versions.get("deployment_adapter"),
            "gateway_version": versions.get("gateway_version"),
            "serving": "api" if vendor else ("self-served" if versions else None),
            "note": API_NOTE if vendor else (SELF_SERVED_NOTE if versions else None),
        }
    )
    return facts


def _perf(inputs: ReleaseInputs) -> dict[str, Any]:
    """``perf.json``: per endpoint, its benchmark and the in-batch request distributions."""
    requests, trial_of = _batch_requests(inputs)
    frozen = (inputs.freeze or {}).get("probe_report_ids") or {}
    endpoints = sorted({*map(str, frozen), *requests, *inputs.benchmarks})
    out: dict[str, Any] = {}
    for endpoint in endpoints:
        bench = inputs.benchmarks.get(endpoint)
        facts = (
            dict(bench.facts) if bench is not None else _trial_facts(inputs, trial_of.get(endpoint))
        )
        out[endpoint] = {
            "facts": facts,
            "note": facts.get("note"),
            "benchmark": (
                {
                    "bench_id": bench.bench_id,
                    "created_at": bench.created_at,
                    "raw_path": bench.raw_path,
                    "raw_sha256": bench.raw_sha256,
                    "result": dict(bench.result),
                }
                if bench is not None
                else None
            ),
            "in_batch": {
                "label": _IN_BATCH_LABEL,
                **observed_metrics(requests.get(endpoint, [])),
            },
        }
    return {
        "batch_id": inputs.report.batch_id,
        "definitions": "PLAN.md, Design: inference performance metrics in every report",
        "endpoints": out,
    }


def _perf_markdown(perf: Mapping[str, Any]) -> str:
    lines = [
        "",
        "## Inference performance",
        "",
        "TTFT: request received until the first chunk with generated text; ITL: gap between "
        "consecutive generated-text chunks; TPOT: (t_last - t_first) / (output tokens - 1); "
        "E2E: request received until the last chunk. Requests that were not streamed have no "
        "TTFT, ITL or TPOT.",
        "",
    ]
    for endpoint, entry in sorted((perf.get("endpoints") or {}).items()):
        facts = entry.get("facts") or {}
        lines += [f"### {endpoint}", "", facts_line(facts) + "."]
        if entry.get("note"):
            lines.append(f"Measured {entry['note']}.")
        lines.append("")
        bench = entry.get("benchmark")
        if bench is None:
            lines += ["No benchmark recorded for this endpoint (spleef models bench).", ""]
        else:
            lines += [f"Benchmark {bench['bench_id']} ({bench['created_at']}):", ""]
            lines += bench_markdown(bench.get("result") or {})
        lines += observed_markdown(entry.get("in_batch") or {})
    return "\n".join(lines)
