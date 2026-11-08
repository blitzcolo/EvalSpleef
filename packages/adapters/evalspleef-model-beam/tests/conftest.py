from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from evalspleef_model_beam.conformance import (
    HF_TOKEN_REF,
    IMAGE,
    TOKEN,
    TOKEN_REF,
    FakeBeamGateway,
    FakeBeamInferenceServer,
    FakeBeamSdk,
    FakeHFChecker,
    FakeLauncher,
)
from evalspleef_model_beam.provider import BeamPodProvider

from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec
from evalspleef.testing.secrets import DictSecretResolver

HF_SECRET = "hf_secret_value_do_not_leak"
REPO = "evalspleef-conformance/public-model"
SHA = "1a2b3c4d" * 5
SLUG = "qwen38-27b"

SpecFactory = Callable[..., ModelSpec]


@pytest.fixture
def server() -> Iterator[FakeBeamInferenceServer]:
    instance = FakeBeamInferenceServer(model=SLUG, api_key=TOKEN)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def gateway(server: FakeBeamInferenceServer) -> FakeBeamGateway:
    instance = FakeBeamGateway(token=TOKEN)
    instance.inference_url = server.base_url.removesuffix("/v1")
    return instance


@pytest.fixture
def sdk(gateway: FakeBeamGateway) -> FakeBeamSdk:
    return FakeBeamSdk(gateway)


@pytest.fixture
def fake_launcher(sdk: FakeBeamSdk) -> FakeLauncher:
    return FakeLauncher(sdk)


@pytest.fixture
def provider(gateway: FakeBeamGateway, fake_launcher: FakeLauncher) -> BeamPodProvider:
    instance = BeamPodProvider(
        "acme",
        {"api_key_ref": TOKEN_REF, "image": IMAGE},
        DictSecretResolver({TOKEN_REF: TOKEN, HF_TOKEN_REF: HF_SECRET}),
        launch_runner=fake_launcher,
        api_transport=gateway.transport(),
    )
    instance.hf_checker = FakeHFChecker()
    return instance


@pytest.fixture
def make_spec() -> SpecFactory:
    def build(*, access: Any = "public", token_ref: str | None = None, **params: Any) -> ModelSpec:
        defaults: dict[str, Any] = {"gpu": "RTX5090", "min_replicas": 1, "max_replicas": 1}
        defaults.update(params)
        return ModelSpec(
            slug=SLUG,
            spec_version=2,
            family="qwen3",
            roles=frozenset({"target"}),
            source=HFSource(repo=REPO, revision=SHA, access=access, token_ref=token_ref),
            deployment=DeploymentSpec(adapter="beam-pod", account="acme", params=defaults),
            declared_context_tokens=32768,
            declared_max_output_tokens=4096,
            pricing_ref="beam-pod",
            license="apache-2.0",
            owner="evalspleef",
        )

    return build
