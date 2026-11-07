from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from evalspleef_model_novita.conformance import (
    API_KEY,
    API_KEY_REF,
    HF_TOKEN_REF,
    FakeHFChecker,
    FakeNovitaInferenceServer,
    SteppingClock,
)

from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec
from evalspleef.testing.secrets import DictSecretResolver

HF_SECRET = "hf_secret_value_do_not_leak"
REPO = "evalspleef-conformance/public-model"
SHA = "1a2b3c4d" * 5

SpecFactory = Callable[..., ModelSpec]


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture
def secrets() -> DictSecretResolver:
    return DictSecretResolver({API_KEY_REF: API_KEY, HF_TOKEN_REF: HF_SECRET})


@pytest.fixture
def checker() -> FakeHFChecker:
    return FakeHFChecker()


@pytest.fixture
def make_spec() -> SpecFactory:
    def build(
        *,
        slug: str = "qwen3-32b",
        family: str = "qwen3",
        access: Any = "public",
        token_ref: str | None = None,
        mirror_repo: str | None = None,
        repo: str = REPO,
        **params: Any,
    ) -> ModelSpec:
        defaults: dict[str, Any] = {
            "model": "org/model",
            "weight_bytes": 16_000_000_000,
            "num_hidden_layers": 32,
            "num_key_value_heads": 8,
            "head_dim": 128,
            "product_id": "prod-h100",
        }
        defaults.update(params)
        return ModelSpec(
            slug=slug,
            spec_version=1,
            family=family,
            roles=frozenset({"target"}),
            source=HFSource(
                repo=repo,
                revision=SHA,
                access=access,
                token_ref=token_ref,
                mirror_repo=mirror_repo,
            ),
            deployment=DeploymentSpec(adapter="novita", account="acme", params=defaults),
            declared_context_tokens=8192,
            declared_max_output_tokens=1024,
            pricing_ref="novita",
            license="apache-2.0",
            owner="evalspleef",
        )

    return build


@pytest.fixture
def inference_server(clock: SteppingClock) -> Iterator[FakeNovitaInferenceServer]:
    server = FakeNovitaInferenceServer(clock=clock, model="org/model", api_key=API_KEY)
    server.start()
    try:
        yield server
    finally:
        server.stop()
