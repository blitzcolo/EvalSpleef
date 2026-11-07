"""Builtin conformance targets running against the evalspleef-testing doubles.

``build_targets`` is registered under the ``evalspleef.conformance_targets``
entry-point group. All four targets set ``fake=True``: they exercise the
conformance machinery itself, not a real provider host.
"""

from __future__ import annotations

from collections.abc import Mapping

from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.model import (
    DeploymentSpec,
    ModelSpec,
    VendorSource,
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


def _model_spec_factory() -> ModelSpec:
    return ModelSpec(
        slug="mock-model",
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="mock", model_id="fake-model"),
        deployment=DeploymentSpec(
            adapter="builtin",
            account="default",
            params={"base_url": "https://mock.invalid/v1", "model": "fake-model"},
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="mock",
        license="proprietary",
        owner="evalspleef",
    )


def _operator_input_factory() -> Mapping[str, str]:
    return {
        "base_url": "https://operator.example/v1",
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
) -> ConformanceTarget:
    return ConformanceTarget(
        name=f"mock-model-{provider.management_mode.value}",
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=_model_spec_factory,
            operator_input_factory=(
                _operator_input_factory if isinstance(provider, MockAssistedProvider) else None
            ),
            upstream=None,
        ),
        notes="management-plane double; no live upstream server is started",
    )


def _secrets() -> DictSecretResolver:
    return DictSecretResolver({"vault://models/mock-model": "sk-mock-conformance"})


def build_targets() -> list[ConformanceTarget]:
    """The four builtin targets: the mock sandbox and the three mock model providers."""
    return [
        _mock_sandbox_target(),
        _model_target(MockApiProvider("default", {}, _secrets())),
        _model_target(MockAssistedProvider("default", {}, _secrets())),
        _model_target(MockExternalProvider("default", {}, _secrets())),
    ]
