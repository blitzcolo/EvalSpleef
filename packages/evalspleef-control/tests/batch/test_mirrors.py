from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from evalspleef.control.batch.mirrors import verify_mirrors
from evalspleef.control.config import load_models
from evalspleef.control.events import open_event_store
from evalspleef.core.events import EventType
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec, ValidationReport, VendorSource

SHA = "a" * 40


class _Mirrors(HFAccessChecker):
    def __init__(self, moved: set[str]) -> None:
        self.moved = moved
        self.asked: list[str] = []

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        return revision

    async def preflight(self, source: HFSource) -> ValidationReport:
        return ValidationReport(ok=True)

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return f"mirror-{source.repo}"

    async def verify_mirror(self, source: HFSource) -> bool:
        assert source.mirror_repo is not None
        self.asked.append(source.mirror_repo)
        return source.mirror_repo not in self.moved


def _spec(slug: str, source: HFSource | VendorSource) -> ModelSpec:
    return ModelSpec(
        slug=slug,
        spec_version=1,
        family="f",
        roles=frozenset({"target"}),
        source=source,
        deployment=DeploymentSpec(adapter="openai-compatible", account="a"),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="p",
        license="mit",
        owner="o",
    )


async def test_a_moved_mirror_opens_one_incident(tmp_path: Path) -> None:
    models = [
        _spec("kept", HFSource(repo="o/kept", revision=SHA, access="public", mirror_repo="m/kept")),
        _spec(
            "moved", HFSource(repo="o/moved", revision=SHA, access="public", mirror_repo="m/moved")
        ),
        _spec("plain", HFSource(repo="o/plain", revision=SHA, access="public")),
        _spec("hosted", VendorSource(vendor="v", model_id="x")),
    ]
    checker = _Mirrors(moved={"m/moved"})
    events = await open_event_store(f"jsonl://{tmp_path}/events")
    try:
        moved = await verify_mirrors(
            models, checker, events, batch_id="2026-11-b1", ts="2026-11-08T00:00:00Z"
        )
        recorded = await events.read("2026-11-b1")
    finally:
        await events.close()
    assert moved == ["moved"]
    assert checker.asked == ["m/kept", "m/moved"]
    assert [(e.type, e.payload["kind"], e.payload["model"]) for e in recorded] == [
        (EventType.INCIDENT_OPENED, "hf_mirror_changed", "moved")
    ]


def test_the_mirror_choice_of_models_yaml_reaches_the_spec(tmp_path: Path) -> None:
    def entry(slug: str, mirror: bool | None) -> dict[str, object]:
        source: dict[str, object] = {
            "type": "huggingface",
            "repo": "o/r",
            "revision": SHA,
            "access": "public",
        }
        if mirror is not None:
            source["mirror"] = mirror
        return {
            "slug": slug,
            "family": "f",
            "roles": ["target"],
            "source": source,
            "deployment": {"adapter": "openai-compatible", "account": "a"},
            "declared_context_tokens": 8192,
            "declared_max_output_tokens": 1024,
            "pricing_ref": "p",
            "license": "mit",
            "owner": "o",
        }

    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump({"models": [entry("on", True), entry("off", False), entry("unset", None)]})
    )
    params = {spec.slug: spec.deployment.params for spec in load_models(path)}
    assert (params["on"]["mirror"], params["off"]["mirror"], params["unset"]["mirror"]) == (
        True,
        False,
        False,
    )
