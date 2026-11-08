"""spleef models register -> tickets -> attach for the assisted novita-dedicated flow.

The Novita-hosted request of ``attach`` is answered by an ``httpx.MockTransport`` injected
into the adapter constructor; probe traffic goes through the gateway data plane to the fake
OpenAI server, exactly as in ``test_models.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from evalspleef_model_novita.dedicated import NovitaDedicatedProvider
from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.testing.fake_openai import FakeOpenAIServer

UPSTREAM_MODEL = "acme/novita-model"
NOVITA_BASE_URL = "https://fake-endpoint.novita.ai/v1"

MODELS_YAML = f"""\
models:
  - slug: dedicated
    family: demo
    roles: [target]
    source: {{type: vendor, vendor: novita, model_id: {UPSTREAM_MODEL}}}
    deployment:
      adapter: novita-dedicated
      account: novita
      params:
        model: {UPSTREAM_MODEL}
        weight_bytes: 16000000000
        num_hidden_layers: 32
        num_key_value_heads: 8
        head_dim: 128
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:dedicated@2026-11"
    license: MIT
    owner: test
"""

PROVIDERS_YAML = """\
model_accounts:
  novita:
    adapter: novita-dedicated
    api_key_ref: "env://SPLEEF_NOVITA_API_KEY"

sandbox_instances:
  mock:
    adapter: mock-sandbox
    config: {}
    limits: {max_concurrency: 8}

routes:
  - when: {os: linux}
    prefer: [mock]

gateway:
  internal_url: "http://gateway.invalid"
"""


class NovitaHost:
    """Records every request the adapter sends to the Novita-hosted endpoint."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"model": UPSTREAM_MODEL, "choices": []})


@pytest.fixture
def fake_server() -> Iterator[FakeOpenAIServer]:
    """The fake model server answers under the attached upstream name (probe P-14)."""
    server = FakeOpenAIServer(model=UPSTREAM_MODEL, parallel_tool_calls=True)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def novita_host(monkeypatch: pytest.MonkeyPatch) -> NovitaHost:
    """Route the adapter's Novita-hosted HTTP calls to a MockTransport."""
    host = NovitaHost()
    original = NovitaDedicatedProvider.__init__

    def init(self: NovitaDedicatedProvider, *a: Any, **kw: Any) -> None:
        original(self, *a, transport=httpx.MockTransport(host.handle), **kw)

    monkeypatch.setattr(NovitaDedicatedProvider, "__init__", init)
    return host


@pytest.fixture
def dedicated_args(env: Path, args: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    (env / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    (env / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    monkeypatch.setenv("SPLEEF_NOVITA_API_KEY", "novita-key")
    return args


def _endpoints(runner: CliRunner, args: list[str]) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["models", "list", "--json", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.output)
    return rows


def _only_endpoint(runner: CliRunner, args: list[str]) -> dict[str, Any]:
    rows = _endpoints(runner, args)
    assert len(rows) == 1, rows
    return rows[0]


def _attach_cmd(endpoint_id: str, base_url: str, args: list[str]) -> list[str]:
    return [
        "models",
        "attach",
        "--endpoint-id",
        endpoint_id,
        "--base-url",
        base_url,
        "--model",
        UPSTREAM_MODEL,
        "--deployment-id",
        "dep-1",
        *args,
    ]


def _stored_reports(store: Path, endpoint_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(store) as db:
        rows = db.execute(
            "SELECT report_id, results FROM probe_report WHERE endpoint_id = ?", (endpoint_id,)
        ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def test_register_attach_probe_ends_ready_with_report(
    cli_runner: CliRunner,
    dedicated_args: list[str],
    novita_host: NovitaHost,
    tmp_path: Path,
) -> None:
    registered = cli_runner.invoke(app, ["models", "register", *dedicated_args])
    assert registered.exit_code == 0, registered.output
    endpoint = _only_endpoint(cli_runner, dedicated_args)
    endpoint_id = endpoint["endpoint_id"]
    assert endpoint["state"] == "awaiting_operator"
    assert endpoint["probe_report"] is None
    assert novita_host.requests == []

    tickets = cli_runner.invoke(app, ["models", "tickets", "--json", *dedicated_args])
    assert tickets.exit_code == 0, tickets.output
    rows = json.loads(tickets.output)
    assert [(r["endpoint_id"], r["kind"], r["status"]) for r in rows] == [
        (endpoint_id, "deploy", "open")
    ]

    attached = cli_runner.invoke(app, _attach_cmd(endpoint_id, NOVITA_BASE_URL, dedicated_args))
    assert attached.exit_code == 0, attached.output

    # exactly one max_tokens=1 request reached the Novita-hosted URL, with the account key
    assert len(novita_host.requests) == 1
    sent = novita_host.requests[0]
    assert str(sent.url) == f"{NOVITA_BASE_URL}/chat/completions"
    assert sent.headers["authorization"] == "Bearer novita-key"
    body = json.loads(sent.content)
    assert body["max_tokens"] == 1
    assert body["model"] == UPSTREAM_MODEL

    endpoint = _only_endpoint(cli_runner, dedicated_args)
    assert endpoint["state"] == "ready"
    report_id = endpoint["probe_report"]
    assert report_id
    stored = _stored_reports(tmp_path / "store.sqlite3", endpoint_id)
    assert [rid for rid, _ in stored] == [report_id]
    assert json.loads(stored[0][1])  # probe results were recorded

    # the deploy ticket was closed by the attach
    open_tickets = cli_runner.invoke(app, ["models", "tickets", "--json", *dedicated_args])
    assert json.loads(open_tickets.output) == []

    listed = cli_runner.invoke(app, ["models", "list", *dedicated_args])
    assert "ready" in listed.output
    assert report_id in listed.output


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:9/v1",  # plain http to loopback: allow_loopback_http is not set
        "https://evil.example.com/v1",  # https but not a *.novita.ai host
        "https://fake-endpoint.novita.ai.evil.example.com/v1",  # suffix trick
    ],
)
def test_attach_rejects_non_novita_host_without_side_effects(
    cli_runner: CliRunner,
    dedicated_args: list[str],
    novita_host: NovitaHost,
    tmp_path: Path,
    bad_url: str,
) -> None:
    assert cli_runner.invoke(app, ["models", "register", *dedicated_args]).exit_code == 0
    endpoint_id = _only_endpoint(cli_runner, dedicated_args)["endpoint_id"]

    result = cli_runner.invoke(app, _attach_cmd(endpoint_id, bad_url, dedicated_args))
    assert result.exit_code != 0, result.output

    assert novita_host.requests == []
    endpoint = _only_endpoint(cli_runner, dedicated_args)
    assert endpoint["state"] == "awaiting_operator"
    assert endpoint["probe_report"] is None
    assert _stored_reports(tmp_path / "store.sqlite3", endpoint_id) == []
    tickets = json.loads(
        cli_runner.invoke(app, ["models", "tickets", "--json", *dedicated_args]).output
    )
    assert [t["status"] for t in tickets] == ["open"]


def test_never_ready_without_attach(
    cli_runner: CliRunner, dedicated_args: list[str], novita_host: NovitaHost
) -> None:
    assert cli_runner.invoke(app, ["models", "register", *dedicated_args]).exit_code == 0
    # a second register is idempotent and must not skip the operator step either
    assert cli_runner.invoke(app, ["models", "register", *dedicated_args]).exit_code == 0
    endpoint = _only_endpoint(cli_runner, dedicated_args)
    assert endpoint["state"] != "ready"
    assert endpoint["probe_report"] is None
    assert novita_host.requests == []

    # probing an endpoint that was never attached cannot make it ready either
    probe = cli_runner.invoke(app, ["models", "probe", *dedicated_args])
    assert _only_endpoint(cli_runner, dedicated_args)["state"] != "ready", probe.output
    assert novita_host.requests == []
