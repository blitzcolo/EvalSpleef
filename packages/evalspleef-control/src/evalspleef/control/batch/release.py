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
from dataclasses import dataclass
from typing import Any

from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.report import BatchReport
from evalspleef.spi.model import ModelSpec

from ..ledger import ReconcileReport
from ..store.rows import GradeRow, ProbeReportRow, TrialRow

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


def build_release(inputs: ReleaseInputs) -> dict[str, str]:
    """File name -> content of every release artifact."""
    return {
        "leaderboard.md": inputs.report.to_markdown(),
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
