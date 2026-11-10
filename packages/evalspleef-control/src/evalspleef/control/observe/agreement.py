"""Judge-agreement and calibration analysis over recorded events (PRD C.5 11.3, AC-09, NFR-01).

Read-side, pure functions: they take what the event log and the task packs recorded
and turn it into the numbers the CLI prints. All statistics come from
:mod:`evalspleef.core.agreement`; this module only shapes the inputs (per-trial,
per-item, per-judge vote tables; label files; grading inputs; cell tiers).

The ``judge.verdict`` payload carries per-judge votes since the calibration lane
landed (``items[].judge_votes[judge] = {decision, samples}``); older events without
the field simply contribute nothing here, which surfaces as "not enough data".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from evalspleef.core.agreement import (
    ALPHA_EXPLORATORY,
    KAPPA_GATE,
    PERCENT_GATE,
    REGRADE_GATE,
    STABILITY_GATE,
    AgreementResult,
    AgreementUndefinedError,
    alpha_verdict,
    cohen_kappa,
    krippendorff_alpha,
    percent_agreement,
)
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.models import TIER_VALUE, CheckKind, Tier

from ..taskpack.models import TaskPack

__all__ = [
    "NotEnoughDataError",
    "VoteTable",
    "agreement_summary",
    "calibration_summary",
    "parse_label_file",
    "regrade_summary",
    "seed_gold_rows",
    "stability_summary",
    "vote_table_from_events",
]

#: trial_id -> item_id -> judge slug -> "pass" / "fail" (inconclusive counts as missing).
type VoteTable = dict[str, dict[str, dict[str, str]]]

#: Keys of one row in a label file (a ``judges export-labels`` artifact).
LABEL_ROW_KEYS = ("trial_id", "task_id", "item_id", "rubric_text", "artifacts", "label")

#: Rubric item kinds the seed gold set covers (judge-decidable items; script/human are out of
#: calibration scope).
_DECIDABLE_KINDS = (CheckKind.LLM_JUDGE, CheckKind.AGENT_JUDGE)


class NotEnoughDataError(ValueError):
    """The recorded data is insufficient to compute this metric (no pairable judge votes / labels)."""


def _result_dict(result: AgreementResult) -> dict[str, Any]:
    return result.to_dict()


def vote_table_from_events(events: Iterable[EventEnvelope]) -> VoteTable:
    """Extract the per-judge pass/fail decisions from ``judge.verdict`` events.

    Later events overwrite earlier ones for the same (trial, item, judge);
    ``inconclusive`` decisions count as missing. Judged items without the
    ``judge_votes`` field (recorded before that payload existed) contribute
    nothing.
    """
    table: VoteTable = {}
    for event in events:
        if event.type is not EventType.JUDGE_VERDICT or not isinstance(event.payload, Mapping):
            continue
        items = event.payload.get("items")
        trial_id = str(event.trial_id or "")
        if not isinstance(items, list) or not trial_id:
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            votes = item.get("judge_votes")
            item_id = str(item.get("item_id", ""))
            if not isinstance(votes, Mapping) or not item_id:
                continue
            for judge, vote in votes.items():
                if not isinstance(vote, Mapping):
                    continue
                decision = vote.get("decision")
                if decision in ("pass", "fail"):
                    table.setdefault(trial_id, {}).setdefault(item_id, {})[str(judge)] = str(
                        decision
                    )
    return table


def _pair_rows(
    units: Sequence[Mapping[str, str]], judge_a: str, judge_b: str
) -> list[tuple[str, str]]:
    return [(unit[judge_a], unit[judge_b]) for unit in units if judge_a in unit and judge_b in unit]


def _alpha_over_units(units: Sequence[Mapping[str, str]], judges: Sequence[str]) -> dict[str, Any]:
    matrix = [[unit.get(judge) for judge in judges] for unit in units]
    try:
        result = krippendorff_alpha(matrix, metric="nominal")
    except AgreementUndefinedError as exc:
        return {"metric": "krippendorff_alpha_nominal", "value": None, "n": 0, "note": str(exc)}
    payload = _result_dict(result)
    payload["verdict"] = alpha_verdict(result.value)
    return payload


def agreement_summary(votes: VoteTable) -> dict[str, Any]:
    """Inter-judge agreement over a batch's recorded verdicts.

    Per judge pair Cohen κ and percent agreement on item verdicts, Krippendorff
    α (nominal) across all judges with missing data, the pooled percent
    agreement, the ``panel_split`` count (items whose judges did not all agree)
    and ``n`` (items rated by at least two judges).
    """
    units = [unit for trial in votes.values() for unit in trial.values() if len(unit) >= 2]
    if not units:
        raise NotEnoughDataError(
            "no item carries votes from at least two judges (panel_split needs a panel)"
        )
    judges = sorted({judge for unit in units for judge in unit})
    pairs: list[dict[str, Any]] = []
    pooled: list[tuple[str, str]] = []
    for index, judge_a in enumerate(judges):
        for judge_b in judges[index + 1 :]:
            rows = _pair_rows(units, judge_a, judge_b)
            if not rows:
                continue
            pooled.extend(rows)
            entry: dict[str, Any] = {
                "judge_a": judge_a,
                "judge_b": judge_b,
                "n": len(rows),
                "percent_agreement": _result_dict(percent_agreement(rows, threshold=None)),
            }
            try:
                entry["cohen_kappa"] = _result_dict(cohen_kappa(rows, threshold=None))
            except AgreementUndefinedError as exc:
                entry["cohen_kappa"] = {"metric": "cohen_kappa", "value": None, "note": str(exc)}
            pairs.append(entry)
    panel_splits = sum(1 for unit in units if len(set(unit.values())) > 1)
    return {
        "n": len(units),
        "judges": judges,
        "pairs": pairs,
        "alpha": _alpha_over_units(units, judges),
        "percent_agreement": _result_dict(percent_agreement(pooled, threshold=None)),
        "panel_splits": panel_splits,
        "gates": {
            "percent_agreement": PERCENT_GATE,
            "cohen_kappa": KAPPA_GATE,
            "alpha_exploratory": ALPHA_EXPLORATORY,
        },
    }


def calibration_summary(votes: VoteTable, labels: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    """Calibrate every judge against a filled label file.

    Per judge: Cohen κ and percent agreement versus the labels on the items
    both rated (the C.5 11.4 gate is the dual gate percent ≥ 0.70 and κ ≥ 0.70,
    AC-09). Across all judges plus the label as one more rater: Krippendorff α
    (nominal, missing data).
    """
    labeled: list[tuple[dict[str, str], str]] = [
        (unit, labels[(trial_id, item_id)])
        for trial_id, items in votes.items()
        for item_id, unit in items.items()
        if (trial_id, item_id) in labels
    ]
    if not labeled:
        raise NotEnoughDataError("no recorded judge verdict matches a label row")
    judges = sorted({judge for unit, _ in labeled for judge in unit})
    rows: list[dict[str, Any]] = []
    for judge in judges:
        pairs = [(unit[judge], label) for unit, label in labeled if judge in unit]
        if not pairs:
            continue
        kappa: dict[str, Any]
        try:
            kappa = _result_dict(cohen_kappa(pairs))
        except AgreementUndefinedError as exc:
            kappa = {"metric": "cohen_kappa", "value": None, "note": str(exc)}
        percent = percent_agreement(pairs)
        kappa_value = kappa.get("value")
        rows.append(
            {
                "judge": judge,
                "n": len(pairs),
                "cohen_kappa": kappa,
                "percent_agreement": _result_dict(percent),
                "passed": bool(
                    percent.value >= PERCENT_GATE
                    and kappa_value is not None
                    and kappa_value >= KAPPA_GATE
                ),
            }
        )
    units_with_label = [{**unit, "__label__": label} for unit, label in labeled if len(unit) >= 1]
    alpha = _alpha_over_units(units_with_label, [*judges, "__label__"])
    unmatched = sum(
        1
        for trial_id, items in votes.items()
        for item_id in items
        if (trial_id, item_id) not in labels
    )
    return {
        "n": len(labeled),
        "judges": rows,
        "alpha": alpha,
        "unmatched_items": unmatched,
        "gates": {"percent_agreement": PERCENT_GATE, "cohen_kappa": KAPPA_GATE},
    }


def parse_label_file(text: str) -> dict[tuple[str, str], str]:
    """Parse a filled labelling sheet (JSONL) into ``(trial_id, item_id) -> label``.

    Rows with a null/absent ``label`` are skipped (the blank sheet exports
    those); ``label`` must be ``pass`` or ``fail``.
    """
    labels: dict[tuple[str, str], str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"label file line {lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"label file line {lineno}: each row must be a JSON object")
        label = row.get("label")
        if label is None:
            continue
        if label not in ("pass", "fail"):
            raise ValueError(
                f"label file line {lineno}: label must be pass/fail/null, got {label!r}"
            )
        trial_id = str(row.get("trial_id", ""))
        item_id = str(row.get("item_id", ""))
        if not trial_id or not item_id:
            raise ValueError(f"label file line {lineno}: trial_id and item_id are required")
        labels[(trial_id, item_id)] = str(label)
    return labels


def regrade_summary(comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate regrade-vs-stored tier comparisons into the AC-09 gate report.

    ``comparisons`` rows carry ``trial_id``, ``stored_tier`` and
    ``recomputed_tier`` (None when the regrade now demands a human). The gate
    is exact tier agreement ≥ 0.95 (AC-09 tier-regrade agreement ≥95%).
    """
    comparable = [row for row in comparisons if row.get("recomputed_tier") is not None]
    matching = sum(1 for row in comparable if row["stored_tier"] == row["recomputed_tier"])
    disagreements = [
        dict(row) for row in comparisons if row.get("recomputed_tier") != row.get("stored_tier")
    ]
    n = len(comparable)
    value = matching / n if n else 0.0
    return {
        "metric": "regrade_tier_agreement",
        "value": value if n else None,
        "n": n,
        "matching": matching,
        "threshold": REGRADE_GATE,
        "passed": (value >= REGRADE_GATE) if n else None,
        "disagreements": disagreements,
    }


def stability_summary(batch_tiers: Sequence[Mapping[str, Tier]]) -> dict[str, Any]:
    """Rerun stability across two or more batches with the same config (NFR-01).

    ``batch_tiers[i]`` maps cell id to the batch's tier for that cell. Only
    cells present in every batch count. The NFR-01 gate is the share of cells
    whose tier differs by at most 1 (P=7 … F=0) ≥ 0.90; exact agreement is
    reported alongside.
    """
    if len(batch_tiers) < 2:
        raise NotEnoughDataError("stability needs at least two batches")
    common = set(batch_tiers[0])
    for tiers in batch_tiers[1:]:
        common &= set(tiers)
    if not common:
        raise NotEnoughDataError("the batches share no cell with a tier in every batch")
    cells: list[dict[str, Any]] = []
    within = 0
    exact = 0
    for cell_id in sorted(common):
        values = [TIER_VALUE[tiers[cell_id]] for tiers in batch_tiers]
        spread = max(values) - min(values)
        cells.append(
            {
                "cell_id": cell_id,
                "tiers": [str(tiers[cell_id]) for tiers in batch_tiers],
                "spread": spread,
            }
        )
        within += spread <= 1
        exact += spread == 0
    n = len(cells)
    within_share = within / n
    return {
        "metric": "rerun_stability",
        "n": n,
        "within_one": within,
        "within_one_share": within_share,
        "exact_agreement": exact / n,
        "threshold": STABILITY_GATE,
        "passed": within_share >= STABILITY_GATE,
        "cells": cells,
    }


def seed_gold_rows(packs: Iterable[TaskPack]) -> list[dict[str, Any]]:
    """Build the seed gold set from task packs (the docs/03 §11.4 cold-start substitute).

    Reference answers A and B pass every judge-decidable rubric item; the empty
    agent fails them (the pack selftest proves the same for the hidden tests).
    This is a seed set, weaker than human labels: it carries no real trial and
    no artifact, only the expected verdict per (sample, item).
    """
    rows: list[dict[str, Any]] = []
    for pack in packs:
        items = [item for item in pack.rubric.items if item.kind in _DECIDABLE_KINDS]
        if not items:
            continue
        samples = (
            ("reference-a", "pass", "answers/reference-a"),
            ("reference-b", "pass", "answers/reference-b"),
            ("empty-agent", "fail", "answers/empty-agent"),
        )
        for sample, label, evidence in samples:
            for item in items:
                rows.append(
                    {
                        "sample_id": f"{pack.task_id}/{sample}",
                        "task_id": pack.task_id,
                        "item_id": item.id,
                        "rubric_text": item.judge_question or item.description,
                        "label": label,
                        "evidence": evidence,
                        "seed_set": True,
                    }
                )
    return rows
