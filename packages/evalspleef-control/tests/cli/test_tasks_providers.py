"""spleef tasks lint and spleef providers list/check."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from evalspleef.control.cli.app import app

REPO = Path(__file__).resolve().parents[4]


def test_lint_real_packs_pass(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["lint", "--tasks-dir", str(REPO / "tasks")])
    assert result.exit_code == 0, result.output
    assert "logic-001" in result.output


def test_lint_failure_is_domain_failure(cli_runner: CliRunner, tmp_path: Path) -> None:
    pack = tmp_path / "tasks" / "bad-001"
    pack.mkdir(parents=True)
    (pack / "task.yaml").write_text("id: bad-001\n", encoding="utf-8")
    result = cli_runner.invoke(app, ["lint", "--tasks-dir", str(tmp_path / "tasks")])
    assert result.exit_code == 1


def test_providers_list(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(app, ["providers", "list", *args])
    assert result.exit_code == 0, result.output
    assert "mock-sandbox" in result.output
    assert "openai-compatible" in result.output
    as_json = cli_runner.invoke(app, ["providers", "list", "--json", *args])
    assert as_json.exit_code == 0
    data = json.loads(as_json.output)
    assert any(item["instance"] == "mock" for item in data["sandbox_instances"])


def test_providers_check(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(app, ["providers", "check", *args])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_hosts_list_empty(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(app, ["hosts", "list", *args])
    assert result.exit_code == 0
    assert "no hosts" in result.output
