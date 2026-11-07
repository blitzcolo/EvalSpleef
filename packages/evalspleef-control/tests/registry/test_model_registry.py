"""Tests for the model registry."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.registry import (
    ModelNotFoundError,
    ModelRegistry,
    SameFamilyJudgeError,
    model_spec_from_row,
    model_spec_to_row,
)
from evalspleef.control.store import ModelSpecExistsError, ModelSpecRepository, connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec, VendorSource

NOW = "2026-11-06T12:00:00Z"
SHA = "3f9c2b7d1e" * 4


def _hf_spec(slug: str, version: int, family: str = "qwen") -> ModelSpec:
    return ModelSpec(
        slug=slug,
        spec_version=version,
        family=family,
        roles=frozenset({"target"}),
        source=HFSource(
            repo="our-org/model",
            revision=SHA,
            access="private",
            token_ref="vault://secret/hf/eval",
            files_sha256={"config.json": "abc123"},
        ),
        deployment=DeploymentSpec(adapter="novita-dedicated", account="novita-main"),
        declared_context_tokens=131072,
        declared_max_output_tokens=16384,
        pricing_ref="ref:x@2026-11",
        license="apache-2.0",
        owner="model-ops",
    )


def _vendor_spec(slug: str, version: int, family: str = "claude") -> ModelSpec:
    return ModelSpec(
        slug=slug,
        spec_version=version,
        family=family,
        roles=frozenset({"judge", "target"}),
        source=VendorSource(vendor="anthropic", model_id="claude-sonnet-5"),
        deployment=DeploymentSpec(adapter="openai-compatible", account="anthropic"),
        declared_context_tokens=200000,
        declared_max_output_tokens=64000,
        pricing_ref="api:anthropic@2026-11",
        license="proprietary",
        owner="eval-lead",
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def registry(db: Database) -> ModelRegistry:
    return ModelRegistry(ModelSpecRepository(db), now=lambda: NOW)


async def test_register_and_get_roundtrip(registry: ModelRegistry) -> None:
    spec = _hf_spec("qwen3-32b-ft-sql", 1)
    await registry.register(spec)
    loaded = await registry.get("qwen3-32b-ft-sql", 1)
    assert loaded == spec


async def test_register_is_immutable(registry: ModelRegistry) -> None:
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 1))
    with pytest.raises(ModelSpecExistsError):
        await registry.register(_hf_spec("qwen3-32b-ft-sql", 1))
    # a new spec_version is the only way to change a spec
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 2))
    assert (await registry.get("qwen3-32b-ft-sql", 1)) is not None
    assert (await registry.get("qwen3-32b-ft-sql", 2)) is not None


async def test_get_latest_and_list(registry: ModelRegistry) -> None:
    assert await registry.get_latest("missing") is None
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 1))
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 2))
    await registry.register(_vendor_spec("claude-sonnet-5", 1))
    latest = await registry.get_latest("qwen3-32b-ft-sql")
    assert latest is not None
    assert latest.spec_version == 2
    versions = await registry.list("qwen3-32b-ft-sql")
    assert [s.spec_version for s in versions] == [1, 2]
    assert len(await registry.list("claude-sonnet-5")) == 1


async def test_resolve_slug_at_version(registry: ModelRegistry) -> None:
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 1))
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 2))
    assert (await registry.resolve("qwen3-32b-ft-sql@1")).spec_version == 1
    assert (await registry.resolve("qwen3-32b-ft-sql@2")).spec_version == 2
    assert (await registry.resolve("qwen3-32b-ft-sql")).spec_version == 2


async def test_resolve_missing(registry: ModelRegistry) -> None:
    with pytest.raises(ModelNotFoundError):
        await registry.resolve("nope@1")
    with pytest.raises(ModelNotFoundError):
        await registry.resolve("nope")
    with pytest.raises(ModelNotFoundError) as excinfo:
        await registry.resolve("nope@latest")
    assert "non-integer" in str(excinfo.value)
    with pytest.raises(ModelNotFoundError):
        await registry.resolve("")


async def test_created_at_is_recorded(registry: ModelRegistry, db: Database) -> None:
    await registry.register(_hf_spec("qwen3-32b-ft-sql", 1))
    row = await ModelSpecRepository(db).get("qwen3-32b-ft-sql", 1)
    assert row is not None
    assert row.created_at == NOW


async def test_judge_family_rule(registry: ModelRegistry) -> None:
    target = _hf_spec("qwen3-32b-ft-sql", 1, family="qwen")
    judge_ok = _vendor_spec("claude-sonnet-5", 1, family="claude")
    judge_bad = _hf_spec("qwen-judge", 1, family="qwen")

    assert ModelRegistry.judge_family_check(judge_ok, target) == []
    reasons = ModelRegistry.judge_family_check(judge_bad, target)
    assert len(reasons) == 1
    assert "'qwen'" in reasons[0]

    registry.require_distinct_judge_family(judge_ok, target)
    with pytest.raises(SameFamilyJudgeError):
        registry.require_distinct_judge_family(judge_bad, target)


def test_row_serialization_roundtrip() -> None:
    for spec in (_hf_spec("qwen3-32b-ft-sql", 3), _vendor_spec("claude-sonnet-5", 2)):
        row = model_spec_to_row(spec, created_at=NOW)
        assert model_spec_from_row(row) == spec


def test_row_serialization_rejects_unknown_source() -> None:
    from evalspleef.control.store.rows import ModelSpecRow

    row = ModelSpecRow(
        slug="x",
        spec_version=1,
        family="x",
        roles=("target",),
        source={"type": "custom"},
        deployment={"adapter": "a", "account": "b"},
        declared_context_tokens=1,
        declared_max_output_tokens=1,
        pricing_ref="p",
        license="l",
        owner="o",
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="unknown model source type"):
        model_spec_from_row(row)
