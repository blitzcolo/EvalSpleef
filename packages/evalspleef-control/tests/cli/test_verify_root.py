"""The verify root hidden tests go to follows the configured carriers, or the operator."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalspleef.control.cli.context import AppContext, Settings
from evalspleef.control.cli.errors import UsageError

MOUNTING = "  mounting:\n    adapter: mock-sandbox\n    config: {}\n"
PATHLESS = (
    "  pathless-{n}:\n    adapter: mock-sandbox\n"
    "    config: {{writable_paths: false, verify_root: {root}}}\n"
)


def _settings(tmp_path: Path, instances: str) -> Settings:
    (tmp_path / "models.yaml").write_text("models: []\n", encoding="utf-8")
    providers = f"model_accounts: {{}}\nsandbox_instances:\n{instances}routes: []\n"
    (tmp_path / "providers.yaml").write_text(providers, encoding="utf-8")
    return Settings.from_options(
        models=tmp_path / "models.yaml",
        providers=tmp_path / "providers.yaml",
        batch=None,
        tasks_dir=tmp_path,
        templates_registry=None,
        store_url=f"sqlite:///{tmp_path}/store.sqlite3",
        events_url=f"jsonl://{tmp_path}/events",
        state_dir=tmp_path / "state",
        environ={},
    )


async def test_a_carrier_without_declared_paths_sets_the_verify_root(tmp_path: Path) -> None:
    # The mounting carrier can host any root; the other one can only host its own.
    instances = MOUNTING + PATHLESS.format(n=1, root="/workspace/verify")
    async with AppContext(_settings(tmp_path, instances), environ={}) as ctx:
        assert ctx.verify_root() == "/workspace/verify"
    async with AppContext(_settings(tmp_path, MOUNTING), environ={}) as ctx:
        assert ctx.verify_root() == "/verify"


async def test_the_operator_override_wins(tmp_path: Path) -> None:
    instances = PATHLESS.format(n=1, root="/workspace/verify")
    environ = {"SPLEEF_VERIFY_ROOT": "/workspace/elsewhere"}
    async with AppContext(_settings(tmp_path, instances), environ=environ) as ctx:
        assert ctx.verify_root() == "/workspace/elsewhere"


async def test_carriers_needing_different_roots_are_refused(tmp_path: Path) -> None:
    instances = PATHLESS.format(n=1, root="/workspace/verify") + PATHLESS.format(
        n=2, root="/home/agent/verify"
    )
    async with AppContext(_settings(tmp_path, instances), environ={}) as ctx:
        with pytest.raises(UsageError, match="different verify roots"):
            ctx.verify_root()
