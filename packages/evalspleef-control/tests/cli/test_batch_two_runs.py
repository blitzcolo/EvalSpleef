"""NFR-01 rerun stability: two batches with the same config share one result store.

Regression test for the 2026-11-10 defect: trial ids do not include the batch, so a
second batch planned from the same config re-inserted the same trial ids, the ``trial``
projection rows collided and the second batch's grade version 1 insert failed. The fix
keys the projections by (batch_id, trial_id) (the id format is frozen, PRD appendix A.1).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app

BATCH_ONE = "2026-11-b1"
BATCH_TWO = "2026-11-b2"
TRIAL_ID = "logic-001.demo.scripted.e1.t01"


def _scripted_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """One scripted round so the trial reaches verification quickly, like test_batch."""
    from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner

    rounds = [ScriptedRound(files={"answer.json": "{}"}, final_message="done")]
    monkeypatch.setattr(
        "evalspleef.control.cli.context.AppContext.runner_classes",
        lambda self: {"scripted": lambda: ScriptedRunner(rounds)},  # type: ignore[dict-item]
    )


def _run_batch(cli_runner: CliRunner, args: list[str], batch_file: Path) -> None:
    run = cli_runner.invoke(app, ["batch", "run", *args, "--batch", str(batch_file)])
    assert run.exit_code == 0, run.output


def _resolve(cli_runner: CliRunner, batch_id: str, tier: str) -> None:
    resolved = cli_runner.invoke(
        app,
        [
            "trials",
            "resolve",
            "--batch-id",
            batch_id,
            "--trial-id",
            TRIAL_ID,
            "--tier",
            tier,
            "--penalty",
            "2",
            "--reviewer",
            "alice",
            "--reason",
            "judge outage, read by hand",
        ],
    )
    assert resolved.exit_code == 0, resolved.output


def test_two_same_config_batches_coexist_and_stability_reads_the_store(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    _scripted_round(monkeypatch)
    root = Path(env).parent
    # Retire to sleep, not terminate, so the second run can warm the same endpoint.
    batch_one = env / "batch.yaml"
    batch_one.write_text(
        batch_one.read_text(encoding="utf-8").replace(
            "retire_after: terminate", "retire_after: sleep"
        ),
        encoding="utf-8",
    )
    batch_two = env / "batch-2.yaml"
    batch_two.write_text(
        batch_one.read_text(encoding="utf-8").replace(f"batch: {BATCH_ONE}", f"batch: {BATCH_TWO}"),
        encoding="utf-8",
    )

    _run_batch(cli_runner, args, batch_one)
    # The trial pauses for a human; the human grade gives the cell a tier in the store.
    _resolve(cli_runner, BATCH_ONE, "A")
    # A hosted endpoint is retired after its batch (EXTERNAL providers always retire);
    # restoring the row models the operator re-enabling the same deployment, which is
    # the normal rerun path for hosted models.
    with sqlite3.connect(root / "store.sqlite3") as db:
        db.execute("UPDATE endpoint SET state = 'ready' WHERE model_slug = 'demo'")
    _run_batch(cli_runner, args, batch_two)
    _resolve(cli_runner, BATCH_TWO, "A")

    # Each batch lists its own trial with its own grade version.
    for batch_id in (BATCH_ONE, BATCH_TWO):
        listed = cli_runner.invoke(app, ["trials", "list", "--batch-id", batch_id])
        assert listed.exit_code == 0, listed.output
        assert f"{TRIAL_ID}\tscored\tvalid\tv1 A (human)" in listed.output

    # Both rows live in one store under the same trial id, scoped by batch.
    with sqlite3.connect(root / "store.sqlite3") as db:
        rows = db.execute(
            "SELECT batch_id, trial_id, state FROM trial ORDER BY batch_id"
        ).fetchall()
        grades = db.execute(
            "SELECT batch_id, trial_id, version, tier FROM grade ORDER BY batch_id"
        ).fetchall()
    assert rows == [
        (BATCH_ONE, TRIAL_ID, "scored"),
        (BATCH_TWO, TRIAL_ID, "scored"),
    ]
    assert grades == [
        (BATCH_ONE, TRIAL_ID, 1, "A"),
        (BATCH_TWO, TRIAL_ID, 1, "A"),
    ]

    # Rerun stability reads the store directly and passes for identical tiers.
    stable = cli_runner.invoke(
        app, ["batch", "stability", "--batch-id", BATCH_ONE, "--batch-id", BATCH_TWO]
    )
    assert stable.exit_code == 0, stable.output
    assert "logic-001.demo.scripted" in stable.output
    assert "within-1 share: 1.000 (1/1, gate >= 0.9): PASS" in stable.output

    # Voiding one batch's trial leaves the other batch's row untouched.
    voided = cli_runner.invoke(
        app,
        [
            "trials",
            "void",
            "--batch-id",
            BATCH_ONE,
            "--trial-id",
            TRIAL_ID,
            "--reason",
            "task bug found after release",
        ],
    )
    assert voided.exit_code == 0, voided.output
    for batch_id, state in ((BATCH_ONE, "invalid"), (BATCH_TWO, "scored")):
        listed = cli_runner.invoke(app, ["trials", "list", "--batch-id", batch_id])
        assert f"{TRIAL_ID}\t{state}" in listed.output
