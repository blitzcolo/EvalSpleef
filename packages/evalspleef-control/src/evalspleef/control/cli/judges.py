"""``spleef judges`` — judge-panel agreement, blind labelling and calibration.

Implements the judge half of PRD appendix C.5 (11.2/11.3), AC-09 (κ ≥ 0.70
calibration gate) and the seed gold set of docs/03 §11.4. Every command reads
the recorded ``judge.verdict`` events (the event log is authoritative) and uses
the same store/config loading as the other spleef commands.

Exit codes: 0 gate passed, 1 below the gate, 3 not enough data in the store.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
from typing import Any

import typer

from evalspleef.control.observe.agreement import (
    NotEnoughDataError,
    agreement_summary,
    calibration_summary,
    parse_label_file,
    seed_gold_rows,
    vote_table_from_events,
)
from evalspleef.control.taskpack import TaskPackError, load_task_pack
from evalspleef.core.agreement import ALPHA_EXPLORATORY, PERCENT_GATE
from evalspleef.core.events import EventType
from evalspleef.core.models import parse_trial_id

from ._shared import (
    EVENTS_OPT,
    INCLUDE_OPT,
    JSON_OPT,
    STATE_OPT,
    STORE_OPT,
    TASKS_OPT,
    emit_json,
    emit_table,
    run_coro,
    store_settings,
)
from .context import AppContext
from .errors import EXIT_DOMAIN, EXIT_SUCCESS, DomainError, NoDataError, UsageError

__all__ = ["judges_app"]

judges_app = typer.Typer(no_args_is_help=True)

_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch to analyse")
_EXPORT_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch to export")
_CALIBRATE_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch whose verdicts to calibrate")
_OUT_OPT = typer.Option(..., "--out", help="JSONL labelling sheet to write")
_GOLD_OUT_OPT = typer.Option(..., "--out", help="JSONL gold set to write")
_LABELS_OPT = typer.Option(..., "--labels", help="filled labelling sheet (JSONL)")


def _fmt_metric(entry: dict[str, Any]) -> str:
    value = entry.get("value")
    return "-" if value is None else f"{float(value):.3f}"


def _print_agreement(summary: dict[str, Any]) -> None:
    emit_table(
        ["judge pair", "n", "kappa", "percent"],
        [
            [
                f"{pair['judge_a']} vs {pair['judge_b']}",
                pair["n"],
                _fmt_metric(pair["cohen_kappa"]),
                _fmt_metric(pair["percent_agreement"]),
            ]
            for pair in summary["pairs"]
        ],
    )
    alpha = summary["alpha"]
    typer.echo(f"alpha (nominal, all judges): {_fmt_metric(alpha)} ({alpha.get('verdict', '-')})")
    typer.echo(
        f"pooled percent agreement: {_fmt_metric(summary['percent_agreement'])} "
        f"over {summary['n']} items rated by >=2 judges"
    )
    typer.echo(f"panel splits (judges disagreed): {summary['panel_splits']}")


@judges_app.command("agreement")
def judges_agreement(
    batch_id: str = _BATCH_ID_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Inter-judge agreement over a batch's recorded judge.verdict events.

    Per judge pair Cohen kappa on item verdicts, Krippendorff alpha (nominal)
    across all judges, pooled percent agreement and the panel_split count.
    Gate: pooled percent >= 0.70 and alpha >= 0.667 (C.5 11.3). Exit 0 passes,
    1 is below the gate, 3 means no item carries votes from two judges.
    """

    async def run() -> int:
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            votes = vote_table_from_events(await ctx.events.read(batch_id))
            try:
                summary = agreement_summary(votes)
            except NotEnoughDataError as exc:
                raise NoDataError(str(exc)) from exc
            if as_json:
                emit_json(summary)
            else:
                _print_agreement(summary)
            percent_value = summary["percent_agreement"].get("value")
            alpha_value = summary["alpha"].get("value")
            passed = (
                percent_value is not None
                and float(percent_value) >= PERCENT_GATE
                and (alpha_value is None or float(alpha_value) >= ALPHA_EXPLORATORY)
            )
            if not as_json:
                typer.echo(
                    f"gate (percent >= {PERCENT_GATE}, alpha >= {ALPHA_EXPLORATORY}): "
                    + ("PASS" if passed else "BELOW")
                )
            return EXIT_SUCCESS if passed else EXIT_DOMAIN

    run_coro(run())


@judges_app.command("export-labels")
def judges_export_labels(
    batch_id: str = _EXPORT_BATCH_ID_OPT,
    out: Path = _OUT_OPT,
    tasks_dir: Path = TASKS_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
) -> None:
    """Export a blind labelling sheet for a batch (no judge verdicts included).

    One JSONL row per judged (trial, item): {"trial_id", "task_id", "item_id",
    "rubric_text", "artifacts": [{"path", "sha256"}], "label": null}. Fill in
    "label" ("pass"/"fail") per row and feed the file to ``judges calibrate``.
    """

    async def run() -> int:
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            events = await ctx.events.read(batch_id)
            judged: dict[str, set[str]] = {}
            for event in events:
                if event.type is not EventType.JUDGE_VERDICT or not isinstance(event.payload, dict):
                    continue
                items = event.payload.get("items")
                if not isinstance(items, list) or not event.trial_id:
                    continue
                for item in items:
                    if isinstance(item, dict) and item.get("item_id"):
                        judged.setdefault(event.trial_id, set()).add(str(item["item_id"]))
            if not judged:
                raise NoDataError(f"batch {batch_id} recorded no judge.verdict items")
            rows: list[dict[str, Any]] = []
            for trial_id in sorted(judged):
                task_id = parse_trial_id(trial_id).task_id
                try:
                    pack = load_task_pack(tasks_dir / task_id)
                except TaskPackError as exc:
                    raise DomainError(f"cannot label {trial_id}: {exc}") from exc
                artifacts = [
                    {"path": artifact.path, "sha256": artifact.sha256}
                    for artifact in await ctx.artifacts.list_by_trial(trial_id)
                ]
                for item_id in sorted(judged[trial_id]):
                    item = pack.rubric.get(item_id)
                    rows.append(
                        {
                            "trial_id": trial_id,
                            "task_id": task_id,
                            "item_id": item_id,
                            "rubric_text": item.judge_question or item.description,
                            "artifacts": artifacts,
                            "label": None,
                        }
                    )

            def write_sheet() -> None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8",
                )

            await asyncio.to_thread(write_sheet)
            typer.echo(f"wrote {len(rows)} rows to {out}")
            typer.echo(
                'fill "label" (pass/fail) per row, then: spleef judges calibrate --labels ...'
            )
            return EXIT_SUCCESS

    run_coro(run())


@judges_app.command("calibrate")
def judges_calibrate(
    batch_id: str = _CALIBRATE_BATCH_ID_OPT,
    labels: Path = _LABELS_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Calibrate every judge against a filled label file (docs/03 11.4, AC-09).

    Per judge Cohen kappa and percent agreement versus the labels; Krippendorff
    alpha with the label as one more rater. Gate (dual, C.5 11.4): percent
    >= 0.70 and kappa >= 0.70 per judge. Exit 0 passes, 1 means a judge is
    below the gate, 3 means no recorded verdict matches a label row.
    """

    async def run() -> int:
        try:
            text = await asyncio.to_thread(labels.read_text, encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read label file {labels}: {exc}") from exc
        try:
            label_map = parse_label_file(text)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            votes = vote_table_from_events(await ctx.events.read(batch_id))
            try:
                summary = calibration_summary(votes, label_map)
            except NotEnoughDataError as exc:
                raise NoDataError(str(exc)) from exc
            judges = summary["judges"]
            if as_json:
                emit_json(summary)
            else:
                emit_table(
                    ["judge", "n", "kappa", "percent", "gate"],
                    [
                        [
                            row["judge"],
                            row["n"],
                            _fmt_metric(row["cohen_kappa"]),
                            _fmt_metric(row["percent_agreement"]),
                            "pass" if row["passed"] else "BELOW",
                        ]
                        for row in judges
                    ],
                )
                alpha = summary["alpha"]
                typer.echo(
                    f"alpha with label as rater: {_fmt_metric(alpha)} "
                    f"({alpha.get('verdict', '-')}) over {summary['n']} labelled items"
                )
                if summary["unmatched_items"]:
                    typer.echo(f"recorded items without a label: {summary['unmatched_items']}")
            passed = bool(judges) and all(bool(row["passed"]) for row in judges)
            if not as_json:
                typer.echo(
                    "gate (per judge percent >= 0.70 and kappa >= 0.70): "
                    + ("PASS" if passed else "BELOW")
                )
            return EXIT_SUCCESS if passed else EXIT_DOMAIN

    run_coro(run())


@judges_app.command("seed-gold")
def judges_seed_gold(
    out: Path = _GOLD_OUT_OPT,
    tasks_dir: Path = TASKS_OPT,
    include: list[str] = INCLUDE_OPT,
) -> None:
    """Generate a seed gold set from the task packs (docs/03 11.4 cold start).

    Reference answers A and B pass every judge-decidable rubric item; the empty
    agent fails them. This is a SEED set, weaker than human labels: it carries
    expected verdicts, not real trials. Exit 0 wrote rows, 3 means no pack
    yielded a row.
    """
    packs = []
    for candidate in sorted(tasks_dir.iterdir()):
        if not candidate.is_dir():
            continue
        if include and not any(fnmatch.fnmatch(candidate.name, glob) for glob in include):
            continue
        if not (candidate / "rubric.yaml").is_file():
            continue
        try:
            packs.append(load_task_pack(candidate))
        except TaskPackError as exc:
            raise DomainError(f"task pack {candidate.name}: {exc}") from exc
    rows = seed_gold_rows(packs)
    if not rows:
        raise NoDataError(f"no judge-decidable rubric items under {tasks_dir}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    typer.echo(f"wrote {len(rows)} seed-gold rows from {len(packs)} packs to {out}")
    typer.echo("seed set: weaker than human labels (docs/03 11.4); label real trials for the gate")
