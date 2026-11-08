"""Fixtures for the spleef CLI tests: configs, fake gateway admin, fake OpenAI server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer

REPO = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _waive_mock_isolation_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock provider's only non-waived probe is a prose description, not a shell
    command, so the trial preflight can never pass against the stock double. These
    tests exercise the CLI, not the double, so waive every invariant.

    Known upstream defect: evalspleef.testing.mock_sandbox.MockSandboxProvider.
    isolation_profile ships I1 with probe ``list(labels) == 1 per lease`` which
    run_isolation_probes executes as a shell command and therefore always fails.
    """
    from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
    from evalspleef.testing.mock_sandbox import MockSandboxProvider

    def profile(self: MockSandboxProvider) -> IsolationProfile:
        return IsolationProfile(
            provider=self.name,
            declared_isolation=self.capabilities().isolation,
            invariants={
                f"I{number}": InvariantEntry(
                    how="test double",
                    probe=None,
                    waived=True,
                    reason="test double",
                )
                for number in range(1, 17)
            },
        )

    monkeypatch.setattr(MockSandboxProvider, "isolation_profile", profile)


def _origin(server: FakeOpenAIServer) -> str:
    """The gateway data plane expects an origin; the account wants the /v1 base URL."""
    return server.base_url.removesuffix("/v1")


MODELS_YAML = """\
models:
  - slug: demo
    family: demo
    roles: [target]
    source: {type: vendor, vendor: evalspleef, model_id: demo}
    deployment: {adapter: openai-compatible, account: fake, params: {model: demo}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:demo@2026-11"
    license: MIT
    owner: test

  - slug: broken
    family: demo
    roles: [target]
    source: {type: vendor, vendor: evalspleef, model_id: missing-model}
    deployment: {adapter: openai-compatible, account: fake, params: {model: missing-model}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:broken@2026-11"
    license: MIT
    owner: test
"""

PROVIDERS_YAML = """\
model_accounts:
  fake:
    adapter: openai-compatible
    base_url: "{base_url}"
    api_key_ref: "env://SPLEEF_FAKE_API_KEY"

sandbox_instances:
  mock:
    adapter: mock-sandbox
    config: {{}}
    limits: {{max_concurrency: 8}}

routes:
  - when: {{os: linux}}
    prefer: [mock]
  - when: {{role: verifier}}
    prefer: [mock]

gateway:
  internal_url: "{gateway_url}"
"""

BATCH_YAML = """\
batch: 2026-11-b1
models: [demo]
shells: {unified: scripted}
tasks: {include: [logic-001]}
epochs: 1
budget: {max_total_usd: 100}
endpoint_policy: {warm_before_run: true, retire_after: terminate}
judges: []
pricing_table: "2026-11"
"""


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_server() -> Iterator[FakeOpenAIServer]:
    server = FakeOpenAIServer(model="demo", parallel_tool_calls=True)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def fake_admin() -> InMemoryGatewayAdmin:
    return InMemoryGatewayAdmin(version="gw-test")


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
    fake_admin: InMemoryGatewayAdmin,
) -> Iterator[Path]:
    """A config directory plus the environment the CLI processes run with."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    (config / "providers.yaml").write_text(
        PROVIDERS_YAML.format(base_url=fake_server.base_url, gateway_url=_origin(fake_server)),
        encoding="utf-8",
    )
    (config / "batch.yaml").write_text(BATCH_YAML, encoding="utf-8")
    monkeypatch.setenv("SPLEEF_STORE_URL", f"sqlite:///{tmp_path}/store.sqlite3")
    monkeypatch.setenv("SPLEEF_EVENTS_URL", f"jsonl://{tmp_path}/events")
    monkeypatch.setenv("SPLEEF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SPLEEF_GATEWAY_ADMIN_URL", "http://admin.example.test")
    monkeypatch.setenv("SPLEEF_GATEWAY_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("SPLEEF_GATEWAY_URL", _origin(fake_server))
    monkeypatch.setenv("SPLEEF_FAKE_API_KEY", "fake-key")
    # The CLI builds its HTTP admin client from this class; swap in the in-memory double.
    monkeypatch.setattr(
        "evalspleef.control.cli.context.HttpGatewayAdmin", lambda url, token: fake_admin
    )
    yield config


@pytest.fixture
def models_yaml() -> str:
    return MODELS_YAML


@pytest.fixture
def providers_yaml() -> str:
    return PROVIDERS_YAML


@pytest.fixture
def batch_yaml() -> str:
    return BATCH_YAML


@pytest.fixture
def args(env: Path) -> list[str]:
    """Common CLI options pointing at the fixture config."""
    return [
        "--models",
        str(env / "models.yaml"),
        "--providers",
        str(env / "providers.yaml"),
        "--tasks-dir",
        str(REPO / "tasks"),
        "--templates-registry",
        str(REPO / "templates" / "registry.json"),
    ]
