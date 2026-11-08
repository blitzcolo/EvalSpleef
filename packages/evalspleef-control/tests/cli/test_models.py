"""spleef models: register, probe, warm, retire, list, tickets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer


def _invoke(runner: CliRunner, args: list[str], command: list[str]) -> object:
    return runner.invoke(app, command + args)


def test_register_and_list(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = _invoke(cli_runner, args, ["models", "register", "--model", "demo"])
    assert result.exit_code == 0, result.output
    assert "demo@1" in result.output

    listed = _invoke(cli_runner, args, ["models", "list"])
    assert listed.exit_code == 0
    assert "demo@1" in listed.output
    assert "ready" not in listed.output  # registered, not probed yet

    as_json = _invoke(cli_runner, args, ["models", "list", "--json"])
    assert as_json.exit_code == 0
    rows = json.loads(as_json.output)
    assert any(row["model"] == "demo@1" for row in rows)


def test_register_rejected_is_domain_failure(
    cli_runner: CliRunner, args: list[str], env: Path, fake_server: FakeOpenAIServer
) -> None:
    # The upstream does not advertise `missing-model`, so validation must reject it.
    del fake_server
    result = _invoke(cli_runner, args, ["models", "register", "--model", "broken"])
    assert result.exit_code == 1
    assert "broken" in result.output


def test_probe_marks_ready_and_stores_report(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    fake_admin: InMemoryGatewayAdmin,
) -> None:
    assert _invoke(cli_runner, args, ["models", "register", "--model", "demo"]).exit_code == 0
    result = _invoke(cli_runner, args, ["models", "probe", "--model", "demo"])
    assert result.exit_code == 0, result.output
    assert "probes" in result.output
    listed = _invoke(cli_runner, args, ["models", "list"])
    assert "ready" in listed.output
    # the probe registered a gateway route with the endpoint's public name
    routes = fake_admin.routes
    assert any(name.startswith("ep_demo_") for name in routes)


def test_warm_and_retire(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    assert _invoke(cli_runner, args, ["models", "register", "--model", "demo"]).exit_code == 0
    assert _invoke(cli_runner, args, ["models", "probe", "--model", "demo"]).exit_code == 0
    warm = _invoke(cli_runner, args, ["models", "warm", "--model", "demo"])
    assert warm.exit_code == 0, warm.output
    assert "warm" in warm.output
    retire = _invoke(
        cli_runner, args, ["models", "retire", "--model", "demo", "--policy", "terminate"]
    )
    assert retire.exit_code == 0, retire.output
    assert "retired" in retire.output


def test_tickets_empty(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = _invoke(cli_runner, args, ["models", "tickets"])
    assert result.exit_code == 0
    assert "no tickets" in result.output


def test_probe_without_registration_is_domain_failure(
    cli_runner: CliRunner, args: list[str]
) -> None:
    result = _invoke(cli_runner, args, ["models", "probe", "--model", "demo"])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_unset_placeholder_is_usage_error(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    providers_yaml: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (env / "providers.yaml").write_text(
        providers_yaml.format(base_url="${SPLEEF_NOPE}", gateway_url="${SPLEEF_GATEWAY_URL}"),
        encoding="utf-8",
    )
    monkeypatch.delenv("SPLEEF_NOPE", raising=False)
    result = _invoke(cli_runner, args, ["models", "register", "--model", "demo"])
    assert result.exit_code == 2
    assert "SPLEEF_NOPE" in result.output
