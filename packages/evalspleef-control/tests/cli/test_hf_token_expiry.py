"""The CLI's endpoint manager freezes HF models with the token expiry recorded in models.yaml."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from evalspleef.control.cli.context import AppContext, Settings
from evalspleef.control.store.rows import EndpointRow

SHA = "3f9c2b7d1e" * 4
TOKEN = "env://HF_EVAL_TOKEN"
ENDPOINT = "ep_ft-model_0000aaaa"
PROVIDERS_YAML = """\
model_accounts:
  novita-main:
    adapter: novita-dedicated
    api_key_ref: "env://NOVITA_API_KEY"
sandbox_instances:
  mock:
    adapter: mock-sandbox
    config: {}
routes:
  - when: {os: linux}
    prefer: [mock]
"""


def _models_yaml(token_lines: str) -> str:
    return f"""
models:
  - slug: ft-model
    family: qwen
    roles: [target]
    source:
      type: huggingface
      repo: our-org/ft-model
      revision: {SHA}
      access: private
      token_ref: {TOKEN}
{token_lines}
    deployment: {{adapter: novita-dedicated, account: novita-main}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: ref:novita/ft-model@2026-11
    license: apache-2.0
    owner: model-ops
"""


async def _freeze_guard(tmp_path: Path, token_lines: str) -> tuple[bool, tuple[str, ...]]:
    models = tmp_path / "models.yaml"
    models.write_text(_models_yaml(token_lines), encoding="utf-8")
    providers = tmp_path / "providers.yaml"
    providers.write_text(PROVIDERS_YAML, encoding="utf-8")
    settings = Settings.from_options(
        models=models,
        providers=providers,
        batch=None,
        tasks_dir=tmp_path,
        templates_registry=None,
        store_url=f"sqlite:///{tmp_path}/store.sqlite3",
        events_url=f"jsonl://{tmp_path}/events",
        state_dir=tmp_path / "state",
        environ={},
    )
    async with AppContext(settings, environ={}) as ctx:
        (spec,) = ctx.load_model_specs()
        await ctx.model_registry.register(spec)
        await ctx.endpoints.upsert(
            EndpointRow(
                endpoint_id=ENDPOINT,
                model_slug=spec.slug,
                model_spec_version=spec.spec_version,
                adapter="novita-dedicated",
                account="novita-main",
                state="ready",
                external_ids={},
                observed={"base_url": "https://abc.novita.ai/v1"},
                created_at="2026-11-08T00:00:00Z",
            )
        )
        report = await ctx.endpoint_manager().freeze_guard(
            [ENDPOINT], planned_end=date(2026, 12, 31)
        )
    return report.ok, report.blockers


@pytest.mark.parametrize("expiry", ["2027-06-30", "never"])
async def test_a_recorded_expiry_past_the_batch_lets_the_hf_model_freeze(
    tmp_path: Path, expiry: str
) -> None:
    ok, blockers = await _freeze_guard(tmp_path, f"      token_expires: {expiry}")
    assert ok, blockers


async def test_an_expiry_inside_the_margin_blocks_the_freeze(tmp_path: Path) -> None:
    ok, blockers = await _freeze_guard(tmp_path, "      token_expires: 2027-01-05")
    assert not ok
    assert TOKEN in blockers[0]


async def test_an_unrecorded_expiry_blocks_and_says_what_to_set(tmp_path: Path) -> None:
    ok, blockers = await _freeze_guard(tmp_path, "")
    assert not ok
    assert "token_expires" in blockers[0]
