from __future__ import annotations

from typing import Any

from evalspleef.control.cli.context import price_for_spec
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec


def _spec(pricing: dict[str, Any]) -> ModelSpec:
    return ModelSpec(
        slug="m",
        spec_version=1,
        family="f",
        roles=frozenset({"target"}),
        source=HFSource(repo="o/r", revision="a" * 40, access="public", files_sha256={}),
        deployment=DeploymentSpec(adapter="a", account="a", params={"pricing": pricing}),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="api:claude@2026-11",
        license="mit",
        owner="o",
    )


def test_cache_write_price_is_carried_from_the_pricing_params() -> None:
    price = price_for_spec(
        _spec(
            {
                "input_per_mtok": 2,
                "output_per_mtok": 10,
                "cached_per_mtok": 0.1,
                "cache_write_per_mtok": 2.5,
            }
        )
    )
    assert (price.input_per_mtok, price.cached_per_mtok, price.cache_write_per_mtok) == (
        2,
        0.1,
        2.5,
    )


def test_a_missing_cache_write_price_stays_none() -> None:
    price = price_for_spec(_spec({"input_per_mtok": 2, "output_per_mtok": 10}))
    assert price.cache_write_per_mtok is None
