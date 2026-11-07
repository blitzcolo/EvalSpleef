"""Tests for the template registry and the cross-provider fingerprint comparison."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.store import TemplateBuildRepository, connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.control.templates import (
    STATUS_READY,
    TemplateBuild,
    TemplateError,
    TemplateRegistry,
    compare_fingerprints,
)

DIGEST = "sha256:" + "a" * 64


def _fingerprint(**overrides: str) -> str:
    components = {
        "packages_sha256": "p1",
        "toolchain": "python3.12",
        "user": "agent:uid1000:nosudo",
        "path": "/usr/local/bin:/usr/bin",
    }
    components.update(overrides)
    return json.dumps(components, sort_keys=True)


def _build(**overrides: object) -> TemplateBuild:
    fields: dict[str, object] = {
        "env_id": "web-001",
        "env_version": "1.0.0",
        "oci_digest": DIGEST,
        "provider_template_ids": {"novita": "tpl_123", "e2b": "tpl_456"},
        "fingerprints": {
            "oci": _fingerprint(),
            "novita": _fingerprint(),
            "e2b": _fingerprint(),
        },
    }
    fields.update(overrides)
    return TemplateBuild(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def registry(db: Database) -> TemplateRegistry:
    return TemplateRegistry(TemplateBuildRepository(db))


async def test_register_and_get_roundtrip(registry: TemplateRegistry) -> None:
    build = _build(status=STATUS_READY)
    await registry.register(build)
    assert await registry.get("web-001", "1.0.0") == build
    assert await registry.get("web-001", "9.9.9") is None


async def test_register_upserts_same_key(registry: TemplateRegistry) -> None:
    await registry.register(_build())
    await registry.register(
        _build(status=STATUS_READY, provider_template_ids={"novita": "tpl_999"})
    )
    loaded = await registry.get("web-001", "1.0.0")
    assert loaded is not None
    assert loaded.status == STATUS_READY
    assert loaded.provider_template_ids == {"novita": "tpl_999"}


async def test_list_by_env_orders_versions(registry: TemplateRegistry) -> None:
    await registry.register(_build(env_version="1.1.0"))
    await registry.register(_build(env_version="1.0.0"))
    await registry.register(_build(env_id="data-001"))
    versions = [b.env_version for b in await registry.list("web-001")]
    assert versions == ["1.0.0", "1.1.0"]


async def test_register_rejects_unpinned_digest(registry: TemplateRegistry) -> None:
    with pytest.raises(TemplateError, match="sha256"):
        await registry.register(_build(oci_digest="TODO(pin-on-host)"))
    with pytest.raises(TemplateError, match="must not be empty"):
        await registry.register(_build(env_version=""))


def test_identical_fingerprints_match() -> None:
    comparison = compare_fingerprints(_build().fingerprints)
    assert comparison.ok
    assert [d.provider for d in comparison.diffs] == ["e2b", "novita"]
    assert all(d.differing == () for d in comparison.diffs)


def test_unexpected_component_drift_fails() -> None:
    fingerprints = {
        "oci": _fingerprint(),
        "novita": _fingerprint(toolchain="python3.11"),
    }
    comparison = compare_fingerprints(fingerprints)
    assert not comparison.ok
    (diff,) = comparison.diffs
    assert diff.differing == ("toolchain",)
    assert diff.unexpected == ("toolchain",)


def test_allowed_drift_is_tolerated_per_provider() -> None:
    fingerprints = {
        "oci": _fingerprint(),
        "novita": _fingerprint(path="/usr/local/bin:/usr/bin:/opt/novita"),
        "e2b": _fingerprint(path="/usr/local/bin:/usr/bin:/opt/novita"),
    }
    comparison = compare_fingerprints(fingerprints, allowed_diffs={"novita": {"path"}})
    by_provider = {d.provider: d for d in comparison.diffs}
    assert by_provider["novita"].ok
    assert by_provider["novita"].differing == ("path",)
    assert not by_provider["e2b"].ok  # tolerance does not leak across providers
    assert not comparison.ok


def test_opaque_fingerprints_compare_as_one_component() -> None:
    comparison = compare_fingerprints({"oci": "abc", "novita": "abd"})
    assert comparison.diffs[0].differing == ("fingerprint",)
    assert compare_fingerprints({"oci": "abc", "novita": "abc"}).ok


def test_missing_baseline_is_an_error() -> None:
    with pytest.raises(TemplateError, match="baseline"):
        compare_fingerprints({"novita": _fingerprint()})


def test_expected_provider_without_fingerprint_is_missing() -> None:
    comparison = compare_fingerprints(
        {"oci": _fingerprint()}, expected_providers=["oci", "novita", "e2b"]
    )
    assert comparison.missing == ("e2b", "novita")
    assert not comparison.ok


async def test_registry_compare_uses_registered_build(registry: TemplateRegistry) -> None:
    await registry.register(_build())
    assert (await registry.compare("web-001", "1.0.0")).ok

    drifted = _build(
        env_version="1.1.0",
        fingerprints={"oci": _fingerprint(), "novita": _fingerprint(user="root")},
    )
    await registry.register(drifted)
    comparison = await registry.compare("web-001", "1.1.0")
    assert not comparison.ok
    assert comparison.missing == ("e2b",)  # template registered, no fingerprint recorded
    assert comparison.diffs[0].unexpected == ("user",)

    allowed = await registry.compare("web-001", "1.1.0", allowed_diffs={"novita": ["user"]})
    assert allowed.diffs[0].ok


async def test_registry_compare_unknown_build(registry: TemplateRegistry) -> None:
    with pytest.raises(TemplateError, match="no template build"):
        await registry.compare("nope", "1.0.0")
