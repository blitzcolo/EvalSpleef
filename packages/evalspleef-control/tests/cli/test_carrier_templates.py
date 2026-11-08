"""The CLI maps a plan's OCI template to the vendor template a provider instance boots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalspleef.control.cli.context import AppContext, Settings
from evalspleef.control.cli.errors import UsageError
from evalspleef.spi.sandbox import TemplateRef

DIGEST = "sha256:" + "ab" * 32
OCI = TemplateRef(kind="oci", ref="llm-eval/logic-001:local", digest=DIGEST)
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


def _settings(tmp_path: Path, registry: dict[str, object]) -> Settings:
    (tmp_path / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (tmp_path / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    (tmp_path / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    return Settings.from_options(
        models=tmp_path / "models.yaml",
        providers=tmp_path / "providers.yaml",
        batch=None,
        tasks_dir=tmp_path,
        templates_registry=tmp_path / "registry.json",
        store_url=f"sqlite:///{tmp_path}/store.sqlite3",
        events_url=f"jsonl://{tmp_path}/events",
        state_dir=tmp_path / "state",
        environ={},
    )


async def test_built_vendor_template_is_booted_by_its_instance(tmp_path: Path) -> None:
    registry = {
        "logic-001": {"tag": OCI.ref, "image_id": DIGEST},
        "templates": {DIGEST: {"novita": {"template_id": "tpl-9", "vendor": "novita"}}},
    }
    async with AppContext(_settings(tmp_path, registry), environ={}) as ctx:
        assert ctx.carrier_template(OCI, "novita") == TemplateRef(
            kind="novita_template", ref="tpl-9", digest=DIGEST
        )
        # Instances without a built template (Docker) boot the OCI image itself.
        assert ctx.carrier_template(OCI, "docker-local") == OCI


async def test_unknown_vendor_in_the_registry_is_refused(tmp_path: Path) -> None:
    registry = {"templates": {DIGEST: {"novita": {"template_id": "tpl-9", "vendor": "acme"}}}}
    async with AppContext(_settings(tmp_path, registry), environ={}) as ctx:
        with pytest.raises(UsageError, match="unknown vendor"):
            ctx.carrier_template(OCI, "novita")
