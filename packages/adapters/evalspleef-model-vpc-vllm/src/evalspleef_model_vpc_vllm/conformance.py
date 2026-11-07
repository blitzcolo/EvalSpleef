"""Conformance targets for the vpc-vllm provider, run against fakes.

This module is the entry point of ``evalspleef.conformance_targets``. It is the only module of
the adapter that may import the conformance kit and the test doubles.
"""

from __future__ import annotations

from collections.abc import Mapping

from evalspleef.conformance.cases_model import (
    HF_BIN_ONLY_REPO,
    HF_GATED_REPO,
    HF_PRIVATE_REPO,
    HF_PUBLIC_REPO,
)
from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelSpec,
    ValidationReport,
)
from evalspleef.testing import fake_openai
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver

from .clock import SteppingClock
from .k8s import InMemoryKubeApi
from .provider import VpcVllmProvider

API_KEY = "sk-vpc-vllm-conformance"
API_KEY_REF = "vault://vpc-vllm/conformance"
HF_TOKEN_REF = "vault://hf/conformance"
MODEL_SLUG = "evalspleef-conformance/public-model"
IMAGE = "vllm/vllm-openai@sha256:" + "a" * 64
_REVISION = "0" * 40
_WEIGHT_BYTES = 2_000_000_000


class FakeHFChecker(HFAccessChecker):
    """HF access double that answers for the four fixture repos of the M-HF case."""

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        return revision if len(revision) == 40 else _REVISION

    async def preflight(self, source: HFSource) -> ValidationReport:
        checks: tuple[tuple[str, bool, str], ...]
        if source.repo == HF_GATED_REPO:
            checks = (("licence_accepted", False, "gated model: the licence is not accepted"),)
        elif source.repo == HF_PRIVATE_REPO:
            checks = (("repo_readable", False, "private repository: no permission for this token"),)
        elif source.repo == HF_BIN_ONLY_REPO:
            checks = (("safetensors", False, "only .bin weights found; safetensors required"),)
        elif source.repo == HF_PUBLIC_REPO:
            checks = (
                ("repo_readable", True, "readable"),
                ("safetensors", True, "model.safetensors present"),
                ("architectures", True, "architectures set"),
                ("weight_bytes", True, str(_WEIGHT_BYTES)),
            )
        else:
            checks = (("repo_readable", False, f"repository {source.repo} not found"),)
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=checks)

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return f"mirror-{source.repo.rsplit('/', 1)[-1]}"

    async def verify_mirror(self, source: HFSource) -> bool:
        return True


def _spec_factory(server: FakeOpenAIServer) -> ModelSpec:
    server.start()
    return ModelSpec(
        slug=MODEL_SLUG,
        spec_version=1,
        family="qwen",
        roles=frozenset({"target"}),
        source=HFSource(repo=HF_PUBLIC_REPO, revision=_REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="vpc-vllm",
            account="conformance",
            params={
                "backend": "k8s",
                "base_url": server.base_url,
                "image": IMAGE,
                "gpu_count": 1,
            },
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="vpc-vllm",
        license="apache-2.0",
        owner="evalspleef",
    )


def _secrets() -> DictSecretResolver:
    return DictSecretResolver({API_KEY_REF: API_KEY, HF_TOKEN_REF: "hf_conformance_token"})


def _vpc_vllm_k8s_target() -> ConformanceTarget:
    clock = SteppingClock()
    server = FakeOpenAIServer(model=MODEL_SLUG, api_key=API_KEY)
    checker = FakeHFChecker()
    kube_api = InMemoryKubeApi()

    provider = VpcVllmProvider(
        "conformance",
        {
            "api_key_ref": API_KEY_REF,
            "image": IMAGE,
            "wake_poll_interval_s": 0.05,
            "fake": True,
        },
        _secrets(),
        clock=clock,
        kube_api=kube_api,
    )
    provider.hf_checker = checker

    async def upstream_status(
        status: int = 503, *, count: int = 1, seconds: float | None = None
    ) -> None:
        fake_openai._ERRORS.setdefault(status, ("invalid_request_error", f"injected_{status}"))
        if seconds is not None:
            server.inject_error_for(status, seconds=seconds)
        else:
            server.inject_error(status, count=count)

    async def cleanup() -> None:
        server.stop()

    fixture = ModelFixture(
        spec_factory=lambda: _spec_factory(server),
        operator_input_factory=None,
        upstream=server,
        hf_access=checker,
    )

    return ConformanceTarget(
        name="vpc-vllm-fake",
        kind="model",
        provider=provider,
        fake=True,
        injectors={"upstream_status": upstream_status},
        model_fixture=fixture,
        notes=(
            "not verified on a real host: runs against an in-memory KubeApi test double and "
            "a local FakeOpenAIServer"
        ),
        cleanup=cleanup,
    )


def build_targets() -> list[ConformanceTarget]:
    """Return conformance targets for the vpc-vllm adapter."""
    return [_vpc_vllm_k8s_target()]
