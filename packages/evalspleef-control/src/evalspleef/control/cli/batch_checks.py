"""``spleef batch regrade-agreement`` and ``spleef batch stability`` (AC-09, NFR-01).

Both commands read the batch's recorded events (the authoritative log, AGENTS
rule 6) and never write: a regrade agreement check recomputes the pure grading
from the inputs recorded with the grade (the same record ``spleef trials
regrade`` uses) and compares tiers with the latest recorded grade; stability
compares cell tiers across batches.

Exit codes: 0 gate passed, 1 below the gate, 3 not enough data in the store.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from evalspleef.control.batch.operations import recorded_inputs
from evalspleef.control.observe.agreement import (
    NotEnoughDataError,
    regrade_summary,
    stability_summary,
)
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.models import Tier, median_tier, parse_trial_id
from evalspleef.core.report import cell_id_for

from ._shared import (
    EVENTS_OPT,
    JSON_OPT,
    STATE_OPT,
    STORE_OPT,
    emit_json,
    emit_table,
    run_coro,
    store_settings,
)
from .context import AppContext
from .errors import EXIT_DOMAIN, EXIT_NO_DATA, EXIT_SUCCESS, NoDataError

__all__ = ["register_batch_checks"]

_REGRADE_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch whose scored trials to regrade")
_STABILITY_BATCH_ID_OPT = typer.Option(
    ..., "--batch-id", help="two or more batch ids with the same config"
)


def _latest_grades(events: list[EventEnvelope]) -> dict[str, Mapping[str, Any]]:
    """Latest ``GRADE_COMPUTED`` payload per trial (regrades and overrides append)."""
    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.type is EventType.GRADE_COMPUTED and event.trial_id and event.payload:
            latest[event.trial_id] = event.payload
    return latest


def _print_regrade(summary: dict[str, Any], skipped: list[dict[str, str]]) -> None:
    disagreements = summary["disagreements"]
    if disagreements:
        emit_table(
            ["trial", "stored", "recomputed", "stored rule", "recomputed rule"],
            [
                [
                    row["trial_id"],
                    row["stored_tier"],
                    row.get("recomputed_tier") or "manual",
                    row.get("stored_rule_id") or "-",
                    row.get("recomputed_rule_id") or "-",
                ]
                for row in disagreements
            ],
        )
    else:
        typer.echo("disagreements: none")
    for row in skipped:
        typer.echo(f"skipped {row['trial_id']}: {row['reason']}")
    value = summary.get("value")
    rendered = "-" if value is None else f"{float(value):.3f}"
    typer.echo(
        f"regrade tier agreement: {rendered} "
        f"({summary['matching']}/{summary['n']}, gate >= {summary['threshold']}): "
        + summary["gate"]["status"].upper()
    )
    for entry in summary["gate"]["undefined"]:
        typer.echo(f"{entry['metric']} undefined: {entry['reason']}; gate not decided")


def batch_regrade_agreement(
    batch_id: str = _REGRADE_BATCH_ID_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Regrade every graded trial from its recorded inputs and compare tiers.

    The exact tier agreement with the latest recorded grades is the inherited
    v2 gate (AC-09 tier-regrade agreement >= 0.95); disagreements are listed per trial.
    Exit 0 passes, 1 is below the gate, 3 means the agreement is undefined (every
    regrade demands a human, so no comparable tier exists) or the batch recorded
    no gradable trial.
    """

    async def run() -> int:
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            events = await ctx.events.read(batch_id)
            latest = _latest_grades(events)
            if not latest:
                raise NoDataError(f"batch {batch_id} recorded no grade")
            logs: dict[str, list[EventEnvelope]] = {}
            comparisons: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            for trial_id in sorted(latest):
                stored = latest[trial_id]
                recorded_grade = stored.get("grade")
                grade: Mapping[str, Any] = (
                    recorded_grade if isinstance(recorded_grade, Mapping) else {}
                )
                stored_tier = grade.get("tier", stored.get("tier"))
                if not stored_tier:
                    skipped.append({"trial_id": trial_id, "reason": "recorded grade has no tier"})
                    continue
                if trial_id not in logs:
                    logs[trial_id] = await ctx.events.read(batch_id, trial_id=trial_id)
                inputs = recorded_inputs(logs[trial_id])
                if inputs is None:
                    skipped.append({"trial_id": trial_id, "reason": "no recorded grading inputs"})
                    continue
                outcome = inputs.grade(decided_at=str(grade.get("decided_at", "")))
                recomputed = outcome.grade
                comparisons.append(
                    {
                        "trial_id": trial_id,
                        "stored_tier": str(stored_tier),
                        "recomputed_tier": None if recomputed is None else str(recomputed.tier),
                        "stored_rule_id": (grade.get("decision") or {}).get("rule_id")
                        or stored.get("rule_id"),
                        "recomputed_rule_id": None if recomputed is None else outcome.rule_id,
                    }
                )
            if not comparisons:
                raise NoDataError(f"batch {batch_id} has no graded trial with recorded inputs")
            summary = regrade_summary(comparisons)
            summary["skipped"] = skipped
            if summary["passed"] is None:
                # no regrade reproduced a tier: the gate names a metric nobody computed
                summary["gate"] = {
                    "status": "undecided",
                    "thresholds": {"regrade_tier_agreement": summary["threshold"]},
                    "undefined": [{"metric": "regrade_tier_agreement", "reason": summary["note"]}],
                }
            else:
                summary["gate"] = {
                    "status": "pass" if summary["passed"] else "below",
                    "thresholds": {"regrade_tier_agreement": summary["threshold"]},
                    "undefined": [],
                }
            if as_json:
                emit_json(summary)
            else:
                _print_regrade(summary, skipped)
            status = summary["gate"]["status"]
            if status == "undecided":
                return EXIT_NO_DATA
            return EXIT_SUCCESS if status == "pass" else EXIT_DOMAIN

    run_coro(run())


def _cell_tiers(latest: Mapping[str, Mapping[str, Any]]) -> dict[str, Tier]:
    """Cell tiers from the latest recorded grades: median over the cell's trials."""
    tiers: dict[str, list[Tier]] = {}
    for trial_id, payload in latest.items():
        recorded_grade = payload.get("grade")
        grade: Mapping[str, Any] = recorded_grade if isinstance(recorded_grade, Mapping) else {}
        tier = grade.get("tier", payload.get("tier"))
        if not tier:
            continue
        parts = parse_trial_id(trial_id)
        cell = cell_id_for(parts.task_id, parts.model, parts.shell)
        tiers.setdefault(cell, []).append(Tier(str(tier)))
    return {cell: median_tier(cell_tiers) for cell, cell_tiers in tiers.items()}


def batch_stability(
    batch_ids: list[str] = _STABILITY_BATCH_ID_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Rerun stability across two or more batches with the same config (NFR-01).

    Cell tiers are the median of the cell's latest recorded grades. The gate is
    the share of cells whose tier differs by at most 1 >= 0.90 (NFR-01); the
    exact agreement is printed alongside. Exit 0 passes, 1 is below the gate,
    3 means the batches share no cell with a recorded tier.
    """

    async def run() -> int:
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            batch_tiers = [
                _cell_tiers(_latest_grades(await ctx.events.read(batch_id)))
                for batch_id in batch_ids
            ]
            try:
                summary = stability_summary(batch_tiers)
            except NotEnoughDataError as exc:
                raise NoDataError(str(exc)) from exc
            if as_json:
                emit_json(summary)
            else:
                emit_table(
                    ["cell", *batch_ids, "spread"],
                    [[row["cell_id"], *row["tiers"], row["spread"]] for row in summary["cells"]],
                )
                typer.echo(
                    f"within-1 share: {summary['within_one_share']:.3f} "
                    f"({summary['within_one']}/{summary['n']}, gate >= {summary['threshold']}): "
                    + ("PASS" if summary["passed"] else "BELOW")
                )
                typer.echo(f"exact agreement: {summary['exact_agreement']:.3f}")
            return EXIT_SUCCESS if summary["passed"] else EXIT_DOMAIN

    run_coro(run())


def register_batch_checks(batch_app: typer.Typer) -> None:
    """Attach the batch-level agreement commands to the ``spleef batch`` group."""
    batch_app.command("regrade-agreement")(batch_regrade_agreement)
    batch_app.command("stability")(batch_stability)
