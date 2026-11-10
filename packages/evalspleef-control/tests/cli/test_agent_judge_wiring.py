"""The CLI builds an agent judge only when batch.yaml asks, and refuses an unfit carrier first."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from evalspleef.control.batch.executor import PlannedTrial
from evalspleef.control.cli.context import AppContext, Settings
from evalspleef.control.config.batch import AgentJudgeConfig, BatchFileConfig
from evalspleef.control.taskpack import load_task_pack
from evalspleef.spi.errors import PolicyNotEnforceable
from evalspleef.spi.sandbox import TemplateRef

REPO = Path(__file__).resolve().parents[4]
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
ENVIRON = {"SPLEEF_GATEWAY_ADMIN_URL": "http://gw.invalid", "SPLEEF_GATEWAY_ADMIN_TOKEN": "t"}
BATCH = BatchFileConfig(batch="2026-11-b9", models=["m"], judges=["j"], pricing_table="2026-11")


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (tmp_path / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    return Settings.from_options(
        models=tmp_path / "models.yaml",
        providers=tmp_path / "providers.yaml",
        batch=None,
        tasks_dir=REPO / "tasks",
        templates_registry=None,
        store_url=f"sqlite:///{tmp_path}/store.sqlite3",
        events_url=f"jsonl://{tmp_path}/events",
        state_dir=tmp_path / "state",
        environ=ENVIRON,
    )


def _plan(task_id: str) -> SimpleNamespace:
    pack = load_task_pack(REPO / "tasks" / task_id)
    trial = PlannedTrial(
        trial_id=f"{task_id}.m.terminus.e1.t01",
        batch_id=BATCH.batch,
        cell_id=f"{task_id}.m.terminus",
        task_id=task_id,
        model="m",
        shell="terminus",
        epoch=1,
        index=1,
        requirements=pack.requirements,
        template=TemplateRef("oci", "img", "sha256:" + "a" * 64),
        agent_routes=("mock",),
        verifier_routes=("mock",),
    )
    cell = SimpleNamespace(task=SimpleNamespace(task_id=task_id), trials=(trial,))
    return SimpleNamespace(runnable_cells=(cell,))


async def test_no_agent_judge_section_builds_nothing(tmp_path: Path) -> None:
    async with AppContext(_settings(tmp_path), environ=ENVIRON) as ctx:
        assert await ctx.build_agent_judge(BATCH, _plan("backend-001"), "http://gw.invalid") is None


async def test_a_carrier_without_read_only_upload_is_refused_before_any_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = BATCH.model_copy(update={"agent_judge": AgentJudgeConfig()})
    async with AppContext(_settings(tmp_path), environ=ENVIRON) as ctx:
        declared = replace(ctx.providers.capabilities("mock"), read_only_upload=False)
        monkeypatch.setattr(ctx.providers, "capabilities", lambda _name: declared)
        with pytest.raises(PolicyNotEnforceable, match="backend-001"):
            # The gateway URL is unreachable: reaching key issuance would fail differently.
            await ctx.build_agent_judge(batch, _plan("backend-001"), "http://gw.invalid")
