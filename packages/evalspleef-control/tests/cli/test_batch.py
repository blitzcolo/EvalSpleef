"""spleef batch plan/freeze/run/report against the mock sandbox and scripted runner.

The scripted runner entry point is the empty agent: every hard gate fails, so the
cells land on a failing tier; the tests assert the wiring (plan, guards, events,
report shape), not model quality.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app


def _batch_args(env: Path, args: list[str]) -> list[str]:
    return [*args, "--batch", str(env / "batch.yaml")]


def test_plan_after_probe(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["batch", "plan", *_batch_args(env, args)])
    assert result.exit_code == 0, result.output
    assert "logic-001.demo.terminus" not in result.output  # shell is scripted
    assert "logic-001.demo.scripted" in result.output
    as_json = cli_runner.invoke(app, ["batch", "plan", "--json", *_batch_args(env, args)])
    plan = json.loads(as_json.output)
    assert plan["budget"]["within_cap"] is True


def test_plan_requires_registered_models(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = cli_runner.invoke(app, ["batch", "plan", *_batch_args(env, args)])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_freeze_stores_record(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["batch", "freeze", *_batch_args(env, args)])
    assert result.exit_code == 0, result.output
    assert "version matrix hash" in result.output
    assert (Path(env).parent / "state" / "freezes" / "2026-11-b1.json").is_file()


def test_batch_run_and_report(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0

    # The scripted runner replays a round that writes the declared answer file;
    # without it the empty agent still exercises the full feedback loop.
    from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner

    rounds = [
        ScriptedRound(files={"answer.json": "{}"}, final_message="done"),
    ]
    monkeypatch.setattr(
        "evalspleef.control.cli.context.AppContext.runner_classes",
        lambda self: {"scripted": lambda: ScriptedRunner(rounds)},  # type: ignore[dict-item]
    )

    run = cli_runner.invoke(app, ["batch", "run", *_batch_args(env, args)])
    assert run.exit_code == 0, run.output
    assert "批次报告" in run.output

    report = cli_runner.invoke(app, ["batch", "report", "--json", *_batch_args(env, args)])
    assert report.exit_code == 0, report.output
    data = json.loads(report.output)
    assert data["batch_id"] == "2026-11-b1"
    assert len(data["cells"]) == 1
    cell = data["cells"][0]
    assert cell["trial_count"] == 1
    assert cell["status"] in {"scored", "manual", "skip"}
    # trial rows landed in the result store and replay as legal event history
    events_dir = Path(env).parent / "events"
    events_file = events_dir / "2026-11-b1.jsonl"
    assert events_file.is_file()
    lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert any(line["type"] == "trial.created" for line in lines)

    # trial.versions is recorded when the agent starts and copied onto the trial row.
    started = [line for line in lines if line["type"] == "trial.started"]
    assert len(started) == 1
    versions = started[0]["payload"]["versions"]
    assert versions["model_version"] == "demo@1"
    assert versions["model_source"] == "vendor"
    assert versions["deployment_adapter"] == "openai-compatible"
    assert versions["freeze_hash"].startswith("sha256:")
    assert versions["gateway_version"]
    assert versions["probe_report_id"]
    assert versions["endpoint_base_url_hash"].startswith("sha256:")
    assert versions["shell_version"].startswith("scripted@")
    assert versions["sandbox_adapter"] and versions["provider_instance"]
    with sqlite3.connect(Path(env).parent / "store.sqlite3") as db:
        (row_extensions,) = db.execute("SELECT extensions FROM trial").fetchone()
    assert json.loads(row_extensions)["versions"] == versions
