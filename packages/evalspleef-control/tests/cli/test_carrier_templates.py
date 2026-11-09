"""The CLI maps a plan's OCI template to the built template a provider instance boots."""

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
  built:
    adapter: mock-sandbox
    config: {template_kinds: [novita_template]}
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


async def test_built_template_has_the_kind_its_instance_declares(tmp_path: Path) -> None:
    # The entry shape the registry has always had: the kind comes from the carrier,
    # the descriptive vendor field is not consulted.
    registry = {
        "logic-001": {"tag": OCI.ref, "image_id": DIGEST},
        "templates": {DIGEST: {"built": {"template_id": "tpl-9", "vendor": "acme"}}},
    }
    async with AppContext(_settings(tmp_path, registry), environ={}) as ctx:
        assert ctx.carrier_template(OCI, "built") == TemplateRef(
            kind="novita_template", ref="tpl-9", digest=DIGEST
        )
        # Instances without a built template (Docker) boot the OCI image itself.
        assert ctx.carrier_template(OCI, "docker-local") == OCI


async def test_a_recorded_kind_the_instance_does_not_boot_is_refused(tmp_path: Path) -> None:
    entry = {"template_id": "tpl-9", "kind": "e2b_template"}
    registry = {"templates": {DIGEST: {"built": entry}}}
    async with AppContext(_settings(tmp_path, registry), environ={}) as ctx:
        with pytest.raises(UsageError, match="has kind 'e2b_template'"):
            ctx.carrier_template(OCI, "built")
        entry["kind"] = "novita_template"
        (tmp_path / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
        assert ctx.carrier_template(OCI, "built").kind == "novita_template"


async def test_a_built_template_for_an_oci_only_instance_is_refused(tmp_path: Path) -> None:
    registry = {"templates": {DIGEST: {"mock": {"template_id": "tpl-9"}}}}
    async with AppContext(_settings(tmp_path, registry), environ={}) as ctx:
        with pytest.raises(UsageError, match="names no kind"):
            ctx.carrier_template(OCI, "mock")
