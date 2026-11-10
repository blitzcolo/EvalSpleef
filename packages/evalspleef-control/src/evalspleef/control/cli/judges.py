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
    parse_seed_file,
    seed_gold_rows,
    vote_table_from_events,
)
from evalspleef.control.observe.judging import run_seed_calibration
from evalspleef.control.taskpack import TaskPackError, load_task_pack
from evalspleef.core.agreement import ALPHA_EXPLORATORY, KAPPA_GATE, PERCENT_GATE
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
from .context import AppContext, Settings
from .errors import EXIT_DOMAIN, EXIT_NO_DATA, EXIT_SUCCESS, DomainError, NoDataError, UsageError

__all__ = ["judges_app"]

#: Gate status -> process exit code (a gate with an uncomputed metric is not a failure).
_GATE_EXIT = {"pass": EXIT_SUCCESS, "below": EXIT_DOMAIN, "undecided": EXIT_NO_DATA}

judges_app = typer.Typer(no_args_is_help=True)

_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch to analyse")
_EXPORT_BATCH_ID_OPT = typer.Option(..., "--batch-id", help="batch to export")
_CALIBRATE_BATCH_ID_OPT = typer.Option(None, "--batch-id", help="batch whose verdicts to calibrate")
_OUT_OPT = typer.Option(..., "--out", help="JSONL labelling sheet to write")
_GOLD_OUT_OPT = typer.Option(..., "--out", help="JSONL gold set to write")
_LABELS_OPT = typer.Option(None, "--labels", help="filled labelling sheet (JSONL)")
_SEED_OPT = typer.Option(None, "--seed", help="seed gold set (JSONL) to calibrate against")
_MODELS_OPT = typer.Option(Path("config/models.yaml"), "--models", help="models.yaml path")
_PROVIDERS_OPT = typer.Option(
    Path("config/providers.yaml"), "--providers", help="providers.yaml path"
)
_PANEL_BATCH_OPT = typer.Option(
    Path("config/batch.yaml"), "--batch", help="batch.yaml defining the judge panel"
)
_LIMIT_OPT = typer.Option(None, "--limit", min=1, help="judge at most this many seed rows")


def _fmt_metric(entry: dict[str, Any]) -> str:
    value = entry.get("value")
    return "-" if value is None else f"{float(value):.3f}"


def _row_gate(passed: bool | None) -> str:
    """Render one calibration row's verdict; ``None`` means its kappa was undefined."""
    if passed is None:
        return "undecided"
    return "pass" if passed else "BELOW"


def _undefined_metric(metric: str, entry: dict[str, Any], **fields: str) -> dict[str, str]:
    """Machine-readable entry for a gate component the data could not compute."""
    return {"metric": metric, **fields, "reason": str(entry.get("note") or "undefined")}


def _echo_gate(label: str, gate: dict[str, Any]) -> None:
    """Print the gate verdict, naming every component that stayed uncomputed."""
    for entry in gate["undefined"]:
        subject = entry["metric"]
        if "judge" in entry:
            subject += f" for {entry['judge']}"
        typer.echo(f"{subject} undefined: {entry['reason']}; gate not decided")
    typer.echo(f"{label}: {gate['status'].upper()}")


def _agreement_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """Gate for ``judges agreement``: pooled percent >= 0.70 and alpha >= 0.667.

    Undefined alpha means the gate is undecided: a PASS would claim a component
    the data never produced.
    """
    percent = summary["percent_agreement"]
    alpha = summary["alpha"]
    undefined = [
        _undefined_metric(name, entry)
        for name, entry in (("percent_agreement", percent), ("alpha", alpha))
        if entry.get("value") is None
    ]
    if undefined:
        status = "undecided"
    elif float(percent["value"]) >= PERCENT_GATE and float(alpha["value"]) >= ALPHA_EXPLORATORY:
        status = "pass"
    else:
        status = "below"
    return {
        "status": status,
        "thresholds": {"percent_agreement": PERCENT_GATE, "alpha": ALPHA_EXPLORATORY},
        "undefined": undefined,
    }


def _calibration_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """Gate for ``judges calibrate``: per judge percent >= 0.70 and kappa >= 0.70."""
    judges = summary["judges"]
    undefined = [
        _undefined_metric("cohen_kappa", row["cohen_kappa"], judge=str(row["judge"]))
        for row in judges
        if row["cohen_kappa"].get("value") is None
    ]
    if undefined:
        status = "undecided"
    elif judges and all(row["passed"] is True for row in judges):
        status = "pass"
    else:
        status = "below"
    return {
        "status": status,
        "thresholds": {"percent_agreement": PERCENT_GATE, "cohen_kappa": KAPPA_GATE},
        "undefined": undefined,
    }


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
    1 is below the gate, 3 means the gate cannot be decided: no item carries votes
    from two judges, or a gate metric (alpha) is mathematically undefined.
    """

    async def run() -> int:
        async with AppContext(store_settings(store_url, events_url, state_dir)) as ctx:
            votes = vote_table_from_events(await ctx.events.read(batch_id))
            try:
                summary = agreement_summary(votes)
            except NotEnoughDataError as exc:
                raise NoDataError(str(exc)) from exc
            gate = _agreement_gate(summary)
            summary["gate"] = gate
            if as_json:
                emit_json(summary)
            else:
                _print_agreement(summary)
                _echo_gate(f"gate (percent >= {PERCENT_GATE}, alpha >= {ALPHA_EXPLORATORY})", gate)
            return _GATE_EXIT[gate["status"]]

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


def _print_calibration(summary: dict[str, Any], gate: dict[str, Any]) -> None:
    """Human-readable rendering shared by the label and seed calibration modes."""
    emit_table(
        ["judge", "n", "kappa", "percent", "gate"],
        [
            [
                row["judge"],
                row["n"],
                _fmt_metric(row["cohen_kappa"]),
                _fmt_metric(row["percent_agreement"]),
                _row_gate(row["passed"]),
            ]
            for row in summary["judges"]
        ],
    )
    alpha = summary["alpha"]
    typer.echo(
        f"alpha with label as rater: {_fmt_metric(alpha)} "
        f"({alpha.get('verdict', '-')}) over {summary['n']} labelled items"
    )
    if summary["unmatched_items"]:
        typer.echo(f"recorded items without a label: {summary['unmatched_items']}")
    _echo_gate(
        "gate (per judge percent >= 0.70 and kappa >= 0.70)",
        gate,
    )


@judges_app.command("calibrate")
def judges_calibrate(
    batch_id: str | None = _CALIBRATE_BATCH_ID_OPT,
    labels: Path | None = _LABELS_OPT,
    seed: Path | None = _SEED_OPT,
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch_config: Path = _PANEL_BATCH_OPT,
    include: list[str] = INCLUDE_OPT,
    limit: int | None = _LIMIT_OPT,
    tasks_dir: Path = TASKS_OPT,
    store_url: str | None = STORE_OPT,
    events_url: str | None = EVENTS_OPT,
    state_dir: Path | None = STATE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Calibrate every judge against labels (docs/03 11.4, AC-09).

    Two input modes, mutually exclusive:

    * ``--batch-id`` + ``--labels``: the recorded verdicts of a finished batch
      versus a filled labelling sheet.
    * ``--seed``: judges every seed sample live, through the judge panel the
      batch config defines (``--models``/``--providers``/``--batch``), and
      compares the votes with the seed labels. The seed set is weaker than
      human labels: a pass here is a smoke signal, not the AC-09 gate.
      ``--include``/``--limit`` bound how many rows (and so how much spend)
      the panel judges.

    Both modes use the same dual gate (C.5 11.4): percent >= 0.70 and kappa
    >= 0.70 per judge. Exit 0 passes, 1 means a judge is below the gate, 3
    means the gate cannot be decided: no recorded verdict matches a label row,
    or a judge's kappa is mathematically undefined.
    """

    async def run() -> int:
        if seed is not None:
            if labels is not None or batch_id is not None:
                raise UsageError("--seed is mutually exclusive with --labels/--batch-id")
            return await _calibrate_seed(
                seed, models, providers, batch_config, include, limit, tasks_dir, as_json
            )
        if labels is None or batch_id is None:
            raise UsageError("calibrate needs --batch-id + --labels, or --seed")
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
            gate = _calibration_gate(summary)
            summary["gate"] = gate
            if as_json:
                emit_json(summary)
            else:
                _print_calibration(summary, gate)
            return _GATE_EXIT[gate["status"]]

    run_coro(run())


async def _calibrate_seed(
    seed: Path,
    models: Path,
    providers: Path,
    batch_config_path: Path,
    include: list[str],
    limit: int | None,
    tasks_dir: Path,
    as_json: bool,
) -> int:
    """Judge the seed samples with the configured panel and gate the judges.

    The votes table is keyed by ``sample_id`` where the label-sheet mode keys
    by ``trial_id``; :func:`calibration_summary` and the gate treat them the
    same, so the exit codes match the ``--labels`` rules exactly.
    """
    try:
        text = await asyncio.to_thread(seed.read_text, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read seed file {seed}: {exc}") from exc
    try:
        rows = parse_seed_file(text)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if include:
        rows = [row for row in rows if any(fnmatch.fnmatch(row.task_id, g) for g in include)]
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise NoDataError("no seed rows to calibrate (check --include/--limit)")
    settings = Settings.from_options(
        models=models,
        providers=providers,
        batch=batch_config_path,
        tasks_dir=tasks_dir,
        templates_registry=None,
        store_url=None,
        events_url=None,
        state_dir=None,
    )
    async with AppContext(settings) as ctx:
        batch_config = ctx.load_batch_config()
        if not batch_config.judges:
            raise UsageError(f"batch config {batch_config_path} names no judges")
        clients = await ctx.build_judges(batch_config, ctx.gateway_data_url())
        try:
            result = await asyncio.to_thread(
                run_seed_calibration,
                clients,
                rows,
                tasks_dir=tasks_dir,
                scratch_dir=ctx.settings.state_dir / "judges" / "seed-calibrate",
                max_parallel_per_vendor=batch_config.judge_max_parallel_per_vendor,
                allow_single_vendor=batch_config.allow_single_judge_vendor,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        finally:
            for client in clients.values():
                client.close()
    if result.judged_rows == 0:
        raise NoDataError("no seed row names an llm_judge item the chat panel can judge")
    label_map = {(row.sample_id, row.item_id): row.label for row in rows}
    try:
        summary = calibration_summary(result.votes, label_map)
    except NotEnoughDataError as exc:
        raise NoDataError(str(exc)) from exc
    gate = _calibration_gate(summary)
    summary["gate"] = gate
    summary["seed"] = {
        "rows": len(rows),
        "judged": result.judged_rows,
        "skipped": result.skipped_rows,
        "spend_usd": result.cost_usd,
        "note": "seed gold set: weaker than human labels (docs/03 11.4)",
    }
    if as_json:
        emit_json(summary)
    else:
        _print_calibration(summary, gate)
        typer.echo(
            f"seed calibration: {result.judged_rows} rows judged by the panel, "
            f"{result.skipped_rows} skipped (non-llm_judge items); "
            f"judge spend ${result.cost_usd:.4f}"
        )
        typer.echo(
            "seed labels are weaker than human labels (docs/03 11.4): "
            "a pass here is a smoke signal, not the AC-09 gate"
        )
    return _GATE_EXIT[gate["status"]]


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
