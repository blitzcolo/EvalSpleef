"""A one-vendor judge panel is an explicit batch.yaml exception, never the default (appendix C.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalspleef.control.cli.context import AppContext, Settings
from evalspleef.control.config import load_batch

PROVIDERS_YAML = """\
model_accounts: {}
sandbox_instances:
  mock:
    adapter: mock-sandbox
    config: {}
routes:
  - when: {os: linux}
    prefer: [mock]
"""
BATCH = "batch: 2026-11-b9\nmodels: [m]\njudges: [j]\npricing_table: '2026-11'\n"


def _settings(tmp_path: Path, environ: dict[str, str]) -> Settings:
    (tmp_path / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (tmp_path / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    return Settings.from_options(
        models=tmp_path / "models.yaml",
        providers=tmp_path / "providers.yaml",
        batch=None,
        tasks_dir=tmp_path,
        templates_registry=None,
        store_url=f"sqlite:///{tmp_path}/store.sqlite3",
        events_url=f"jsonl://{tmp_path}/events",
        state_dir=tmp_path / "state",
        environ=environ,
    )


def test_batch_files_default_to_a_two_vendor_panel(tmp_path: Path) -> None:
    path = tmp_path / "batch.yaml"
    path.write_text(BATCH, encoding="utf-8")
    assert load_batch(path).allow_single_judge_vendor is False
    path.write_text(BATCH + "allow_single_judge_vendor: true\n", encoding="utf-8")
    assert load_batch(path).allow_single_judge_vendor is True


@pytest.mark.parametrize("allowed", [False, True])
async def test_the_trial_runner_judges_with_the_batch_policy(tmp_path: Path, allowed: bool) -> None:
    environ = {"SPLEEF_GATEWAY_ADMIN_URL": "http://gw.test", "SPLEEF_GATEWAY_ADMIN_TOKEN": "t"}
    async with AppContext(_settings(tmp_path, environ), environ=environ) as ctx:
        runner = ctx.make_trial_runner(
            gateway_url="http://gw.test", allow_single_judge_vendor=allowed
        )
        assert runner.evaluator._allow_single_judge is allowed
