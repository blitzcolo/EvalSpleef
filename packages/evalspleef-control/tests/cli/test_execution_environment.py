"""spleef batch run records where every trial ran: provider report and measured guests.

Runs the real CLI against the mock sandbox provider. The provider's report is the mock's
fixed fake environment; the guests are measured for real, and mock sandboxes run on this
machine, so a measured guest must name this machine's kernel.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.testing.mock_sandbox import MOCK_ENVIRONMENT

BATCH_ID = "2026-11-b1"


def test_batch_run_records_the_execution_environment_of_every_trial(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner

    batch_args = [*args, "--batch", str(env / "batch.yaml")]
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    rounds = [ScriptedRound(files={"answer.json": "{}"}, final_message="done")]
    monkeypatch.setattr(
        "evalspleef.control.cli.context.AppContext.runner_classes",
        lambda self: {"scripted": lambda: ScriptedRunner(rounds)},  # type: ignore[dict-item]
    )

    run = cli_runner.invoke(app, ["batch", "run", *batch_args])
    assert run.exit_code == 0, run.output
    release = env.parent / "release"
    report = cli_runner.invoke(app, ["batch", "report", "--release", str(release), *batch_args])
    assert report.exit_code == 0, report.output

    expected_provider = {"status": "reported", **MOCK_ENVIRONMENT.to_dict()}
    kernel = os.uname().release

    # trial.started carries the provider report and the agent guest measured at lease time.
    lines = [
        json.loads(line)
        for line in (env.parent / "events" / f"{BATCH_ID}.jsonl").read_text("utf-8").splitlines()
    ]
    (started,) = [line for line in lines if line["type"] == "trial.started"]
    environment = started["payload"]["versions"]["execution_environment"]
    assert environment["provider"] == expected_provider
    agent = environment["sandboxes"]["agent"]
    assert (agent["provider_instance"], agent["status"]) == ("mock", "measured")
    assert agent["guest"]["kernel"] == kernel

    # Every trial row carries it, plus the verifier sandbox measured when it was leased.
    with sqlite3.connect(env.parent / "store.sqlite3") as db:
        rows = [json.loads(ext) for (ext,) in db.execute("SELECT extensions FROM trial")]
    assert rows
    for extensions in rows:
        row_environment = extensions["versions"]["execution_environment"]
        assert row_environment["provider"] == expected_provider
        assert row_environment["sandboxes"]["agent"]["guest"]["kernel"] == kernel
        verifier = row_environment["sandboxes"]["verifier"]
        assert (verifier["status"], verifier["guest"]["kernel"]) == ("measured", kernel)

    # The batch level: the freeze record and the release manifest name the provider report.
    freeze = json.loads((env.parent / "state" / "freezes" / f"{BATCH_ID}.json").read_text())
    assert freeze["provider_environments"] == {"mock": expected_provider}
    manifest = json.loads((release / "versions.json").read_text(encoding="utf-8"))
    assert manifest["provider_environments"] == {"mock": expected_provider}
    drilldown = json.loads((release / "drilldown.json").read_text(encoding="utf-8"))
    assert drilldown
    for cell in drilldown:
        assert cell["execution_environment"]["provider"] == expected_provider
