"""Builtin conformance targets running against the evalspleef-testing doubles.

``build_targets`` is registered under the ``evalspleef.conformance_targets``
entry-point group. All four targets set ``fake=True``: they exercise the
conformance machinery itself, not a real provider host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from evalspleef.conformance.builtin_doubles import (
    FaultAssistedProvider,
    HFDouble,
    LazyUpstreamServer,
    StatusMappingProvider,
)
from evalspleef.conformance.cases_model import HF_PUBLIC_REPO
from evalspleef.conformance.target import ConformanceTarget, Injector, ModelFixture
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelSpec,
)
from evalspleef.spi.sandbox import NetworkPolicy, Resources, Sandbox, SandboxSpec, TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_endpoint import (
    MockApiProvider,
    MockAssistedProvider,
    MockExternalProvider,
)
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

_TEMPLATE_DIGEST = "sha256:" + "0" * 64
_MODEL_SLUG = "mock-model"
_MODEL_SECRET_REF = "vault://models/mock-model"
_MODEL_API_KEY = "sk-mock-conformance"
_OPERATOR_BASE_URL = "https://operator.example/v1"
_HF_REVISION = "0" * 40


def _sandbox_spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref="mock-template", digest=_TEMPLATE_DIGEST),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={"conformance": "builtin"},
    )


def _model_spec(base_url: str) -> ModelSpec:
    return ModelSpec(
        slug=_MODEL_SLUG,
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=HFSource(repo=HF_PUBLIC_REPO, revision=_HF_REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="builtin",
            account="default",
            params={"base_url": base_url, "model": "fake-model"},
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="mock",
        license="proprietary",
        owner="evalspleef",
    )


def _operator_input_factory() -> Mapping[str, str]:
    return {
        "base_url": _OPERATOR_BASE_URL,
        "model": "fake-model",
        "deployment_id": "dep-mock-1",
    }


def _mock_sandbox_target() -> ConformanceTarget:
    clock = FakeClock()
    provider = MockSandboxProvider("mock-sandbox", {}, _secrets(), clock=clock)

    async def lose_sandbox(sandbox: Sandbox) -> None:
        provider.lose(sandbox.id)

    async def fail_create(count: int = 1) -> None:
        provider.fail_next_creates(count, SandboxCreateError)

    async def expire_lifetime() -> None:
        # Move the fake clock past any lifetime an open_sandbox call can request.
        clock.advance(10_000)

    return ConformanceTarget(
        name="mock-sandbox",
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=_sandbox_spec_factory,
        injectors={
            "lose_sandbox": lose_sandbox,
            "fail_create": fail_create,
            "expire_lifetime": expire_lifetime,
        },
        notes="in-process double: real local processes and files, no isolation",
    )


def _model_target(
    provider: MockApiProvider | MockAssistedProvider | MockExternalProvider,
    *,
    spec_factory: Callable[[], ModelSpec],
    operator_input_factory: Callable[[], Mapping[str, str]] | None = None,
    upstream: LazyUpstreamServer | None = None,
    injectors: Mapping[str, Injector] | None = None,
) -> ConformanceTarget:
    async def cleanup() -> None:
        if upstream is not None:
            upstream.stop()

    return ConformanceTarget(
        name=f"mock-model-{provider.management_mode.value}",
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=spec_factory,
            operator_input_factory=operator_input_factory,
            upstream=upstream,
            hf_access=HFDouble(),
        ),
        injectors=dict(injectors or {}),
        notes="management-plane double with in-process upstream and HF fixtures",
        cleanup=cleanup if upstream is not None else None,
    )


def _api_model_target() -> ConformanceTarget:
    provider = StatusMappingProvider("default", {}, _secrets())
    upstream = LazyUpstreamServer(api_key=_MODEL_API_KEY)

    async def upstream_status(status: int) -> None:
        provider.injected_status = status

    return _model_target(
        provider,
        spec_factory=lambda: _model_spec(upstream.base_url),
        upstream=upstream,
        injectors={"upstream_status": upstream_status},
    )


def _assisted_model_target() -> ConformanceTarget:
    provider = FaultAssistedProvider("default", {}, _secrets())

    async def upstream_status(status: int) -> None:
        provider.injected_status = status

    return _model_target(
        provider,
        spec_factory=lambda: _model_spec(_OPERATOR_BASE_URL),
        operator_input_factory=_operator_input_factory,
        injectors={"upstream_status": upstream_status},
    )


def _external_model_target() -> ConformanceTarget:
    provider = MockExternalProvider("default", {}, _secrets())
    upstream = LazyUpstreamServer(api_key=_MODEL_API_KEY)
    return _model_target(
        provider,
        spec_factory=lambda: _model_spec(upstream.base_url),
        upstream=upstream,
    )


def _secrets() -> DictSecretResolver:
    return DictSecretResolver({_MODEL_SECRET_REF: _MODEL_API_KEY})


def build_targets() -> list[ConformanceTarget]:
    """The four builtin targets: the mock sandbox and the three mock model providers."""
    return [
        _mock_sandbox_target(),
        _api_model_target(),
        _assisted_model_target(),
        _external_model_target(),
    ]
