"""spleef models: register, probe, warm, retire, list, tickets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.control.cli.gateway_admin import HttpGatewayAdmin
from evalspleef.control.events import JsonlEventStore
from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.state import GatewayState
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.model import UpstreamRoute
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver


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


async def test_control_admin_client_carries_api_key_header_to_the_gateway() -> None:
    """The CLI's admin client serializes api_key_header into the gateway admin API."""
    store = MemoryStore()
    state = GatewayState(key_store=store, usage_store=store, secrets=DictSecretResolver({}))
    admin_app = create_admin_app(state, "test-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://gw-admin.test"
    ) as client:
        admin = HttpGatewayAdmin("http://gw-admin.test", "test-token", client=client)
        route = UpstreamRoute(
            api_base="http://upstream.test/v1",
            model="fake-model",
            protocol="chat_completions",
            api_key_ref="vault://upstream/key",
            api_key_header="x-goog-api-key",
        )
        await admin.upsert_route("ep_hdr", route)
    entry = state.route_entry("ep_hdr")
    assert entry is not None and entry.route.api_key_header == "x-goog-api-key"


def test_retire_of_an_endpoint_that_never_got_a_route_succeeds(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered but never probed endpoint has no gateway route; retiring it must exit 0.

    The CLI talks to the real admin app here (the stock fixture uses an in-memory double that
    already ignores unknown routes and so cannot show the failure).
    """
    store = MemoryStore()
    state = GatewayState(key_store=store, usage_store=store, secrets=DictSecretResolver({}))
    admin_app = create_admin_app(state, "test-token")
    monkeypatch.setattr(
        "evalspleef.control.cli.context.HttpGatewayAdmin",
        lambda url, token: HttpGatewayAdmin(
            url,
            token,
            client=httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app)),
        ),
    )
    assert _invoke(cli_runner, args, ["models", "register", "--model", "demo"]).exit_code == 0
    assert state.route_entry("ep_demo") is None  # never probed: no route exists

    retire = _invoke(
        cli_runner, args, ["models", "retire", "--model", "demo", "--policy", "terminate"]
    )

    assert retire.exit_code == 0, retire.output
    assert "retired" in retire.output


def test_retire_records_the_engine_log_outcome_in_the_retire_event(
    cli_runner: CliRunner, args: list[str], env: Path, tmp_path: Path
) -> None:
    assert _invoke(cli_runner, args, ["models", "register", "--model", "demo"]).exit_code == 0
    retire = _invoke(
        cli_runner, args, ["models", "retire", "--model", "demo", "--policy", "terminate"]
    )
    assert retire.exit_code == 0, retire.output

    events = asyncio.run(JsonlEventStore(tmp_path / "events", fsync=False).read("endpoints"))

    assert events[-1].payload["state"] == "retired"
    assert events[-1].payload["engine_log"] == {"status": "unsupported"}  # openai-compatible


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
