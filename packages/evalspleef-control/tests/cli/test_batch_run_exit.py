"""`spleef batch run` fails after its report when a runnable cell ended Pending."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import _pending_cells_error, app
from evalspleef.core.report import BatchReport, TrialResult

BATCH = "2026-11-b1"


def _trial(trial_id: str, state: str, validity: str, tier: str | None = None) -> TrialResult:
    record: dict[str, Any] = {"trial_id": trial_id, "state": state, "validity": validity}
    if tier is not None:
        record["grades"] = [
            {"tier": tier, "penalty": 2, "status": "final", "counted_in_leaderboard": True}
        ]
    return TrialResult.from_record(record)


def _scored(cell: str) -> list[TrialResult]:
    return [_trial(f"{cell}.e{epoch}.t01", "scored", "valid", "A") for epoch in (1, 2)]


def test_scored_manual_and_skipped_cells_are_results() -> None:
    report = BatchReport(
        batch_id=BATCH,
        trials=(
            *_scored("logic-001.demo.unified"),
            _trial("data-001.demo.unified.e1.t01", "paused_manual", "manual"),
            _trial("debug-001.demo.unified.e1.t01", "pending", "pending"),
        ),
        skips={"debug-001.demo.unified": "no linux carrier"},
    )
    assert _pending_cells_error(report) is None


def test_a_cell_whose_trials_all_failed_is_named_with_its_trial_states() -> None:
    report = BatchReport(
        batch_id=BATCH,
        trials=(
            *_scored("logic-001.demo.unified"),
            _trial("data-001.demo.unified.e1.t01", "failed_infra", "invalid"),
            _trial("data-001.demo.unified.e1.t02", "invalid", "invalid"),
            _trial("data-001.demo.unified.e2.t01", "invalid", "invalid"),
        ),
    )
    message = _pending_cells_error(report)
    assert message is not None
    assert message.startswith("1 of 2 runnable cell(s) ended Pending")
    assert "(1 failed_infra, 2 invalid)" in message
    assert f"--batch-id {BATCH}" in message


def test_a_run_whose_trials_all_end_invalid_fails_after_the_report(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    from evalspleef.spi.errors import PolicyNotEnforceable
    from evalspleef.testing.mock_sandbox import MockSandboxProvider

    async def refuse(self: MockSandboxProvider, spec: object) -> None:
        raise PolicyNotEnforceable("network policy cannot be enforced")

    monkeypatch.setattr(MockSandboxProvider, "create", refuse)

    run = cli_runner.invoke(app, ["batch", "run", *args, "--batch", str(env / "batch.yaml")])

    assert run.exit_code == 1, run.output
    assert "批次报告" in run.output  # the report is still printed
    assert "error: 1 of 1 runnable cell(s) ended Pending" in run.output
    assert "(1 invalid)" in run.output
