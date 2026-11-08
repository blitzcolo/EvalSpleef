"""Template Builder: OCI image -> vendor template with a JSON digest registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evalspleef_sbx_e2b_family import template_builder
from evalspleef_sbx_e2b_family.template_builder import (
    REMOVE_SUDO_CMD,
    TemplateBuilder,
    TemplateRegistry,
    image_digest,
)

IMAGE = "registry.local/llm-eval/web-001@sha256:" + "ef" * 32


class _FakeTemplateBuilder:
    """Records the chained build steps of the vendor TemplateBuilder."""

    def __init__(self, log: list[tuple[str, tuple, dict]]) -> None:
        self._log = log

    def from_image(self, image: str, **kwargs: object) -> _FakeTemplateBuilder:
        self._log.append(("from_image", (image,), kwargs))
        return self

    def run_cmd(self, command: object, user: object = None) -> _FakeTemplateBuilder:
        self._log.append(("run_cmd", (command,), {"user": user}))
        return self

    def set_user(self, user: str) -> _FakeTemplateBuilder:
        self._log.append(("set_user", (user,), {}))
        return self


class _FakeBuildInfo:
    template_id = "tpl-built-1"


def _install_fake_template_class(monkeypatch: pytest.MonkeyPatch, log, captured_build) -> None:
    class FakeTemplate:
        def __init__(self) -> None:
            self._builder = _FakeTemplateBuilder(log)

        def from_image(self, image: str, **kwargs: object) -> _FakeTemplateBuilder:
            return self._builder.from_image(image, **kwargs)

        @staticmethod
        def build(builder: object, name: str, **kwargs: object) -> _FakeBuildInfo:
            captured_build["builder"] = builder
            captured_build["name"] = name
            captured_build["kwargs"] = kwargs
            return _FakeBuildInfo()

    monkeypatch.setattr(template_builder, "_template_class", lambda vendor: FakeTemplate)


def test_image_digest_requires_pinned_digest() -> None:
    assert image_digest(IMAGE) == "sha256:" + "ef" * 32
    for bad in ("repo/img:latest", "repo/img@md5:abc", "repo/img@sha256:", "repo/img"):
        with pytest.raises(ValueError):
            image_digest(bad)


def test_build_records_registry_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log: list[tuple[str, tuple, dict]] = []
    captured_build: dict = {}
    _install_fake_template_class(monkeypatch, log, captured_build)

    registry = TemplateRegistry(tmp_path / "registry.json")
    builder = TemplateBuilder(
        registry, vendor="novita", provider="novita-sandbox", client_kwargs={"api_key": "k"}
    )
    result = builder.build_from_oci_image(
        IMAGE,
        name="web-001",
        cpus=4,
        memory_mb=8192,
        registry_credentials={"username": "robot", "password": "s3cret"},
    )
    assert result.template_id == "tpl-built-1"
    assert result.digest == "sha256:" + "ef" * 32

    # build pipeline: from_image -> remove sudo as root -> set_user(agent) -> workspace as root
    steps = [entry[0] for entry in log]
    assert steps == ["from_image", "run_cmd", "set_user", "run_cmd"]
    assert log[3][2] == {"user": "root"}
    assert log[0][1] == (IMAGE,)
    assert log[0][2] == {"username": "robot", "password": "s3cret"}
    assert log[1][1] == (REMOVE_SUDO_CMD,)
    assert log[1][2] == {"user": "root"}
    assert log[2][1] == ("agent",)

    # resources are pinned on the template build
    assert captured_build["name"] == "web-001"
    assert captured_build["kwargs"]["cpu_count"] == 4
    assert captured_build["kwargs"]["memory_mb"] == 8192
    assert captured_build["kwargs"]["api_key"] == "k"

    # digest -> template id mapping recorded in the JSON registry
    assert registry.lookup(result.digest, "novita-sandbox") == "tpl-built-1"
    on_disk = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    record = on_disk["templates"][result.digest]["novita-sandbox"]
    assert record["template_id"] == "tpl-built-1"
    assert record["vendor"] == "novita"


def test_registry_lookup_miss(tmp_path: Path) -> None:
    registry = TemplateRegistry(tmp_path / "registry.json")
    assert registry.lookup("sha256:" + "00" * 32, "novita-sandbox") is None


def test_two_providers_share_one_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log: list[tuple[str, tuple, dict]] = []
    captured_build: dict = {}
    _install_fake_template_class(monkeypatch, log, captured_build)
    registry = TemplateRegistry(tmp_path / "registry.json")

    novita = TemplateBuilder(registry, vendor="novita", provider="novita-sandbox")
    novita.build_from_oci_image(IMAGE, name="web-001", cpus=2, memory_mb=4096)
    e2b = TemplateBuilder(registry, vendor="e2b", provider="e2b-selfhost")
    e2b.build_from_oci_image(IMAGE, name="web-001", cpus=2, memory_mb=4096)

    assert registry.lookup(image_digest(IMAGE), "novita-sandbox") == "tpl-built-1"
    assert registry.lookup(image_digest(IMAGE), "e2b-selfhost") == "tpl-built-1"


def test_unknown_vendor_rejected(tmp_path: Path) -> None:
    registry = TemplateRegistry(tmp_path / "registry.json")
    builder = TemplateBuilder(registry, vendor="other", provider="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        builder.build_from_oci_image(IMAGE, name="n", cpus=1, memory_mb=128)
