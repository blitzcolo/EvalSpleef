"""Conformance target for the Kubernetes agent-sandbox provider.

The target is ``k8s-agent-sandbox-fake`` and runs the provider against the
in-memory ``FakeKubeApi``: custom objects are simulated, pod exec runs in real
local subprocesses. ``fake=True`` records in the report that nothing here was
verified against a Kubernetes cluster, a RuntimeClass installation or Cilium.

``reachable_from_sandbox`` is deliberately absent: the fake does not enforce
Cilium egress policies, so the observer-based network cases report
not-applicable instead of guessing.
"""

from __future__ import annotations

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.spi.secrets import SecretRef

from .fake import FakeKubeApi
from .provider import K8sAgentSandboxProvider

_TARGET_NAME = "k8s-agent-sandbox-fake"
_TEMPLATE_REF = "ghcr.io/evalspleef/conformance-sandbox:test"
_TEMPLATE_DIGEST = "sha256:" + "0" * 64


class _EmptySecrets:
    """The provider never resolves secrets; resolution must not be requested."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the k8s agent-sandbox provider does not resolve secrets: {ref!r}")


def _spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref=_TEMPLATE_REF, digest=_TEMPLATE_DIGEST),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": "conformance",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": _TARGET_NAME,
        },
    )


def build_targets() -> list[ConformanceTarget]:
    """The ``k8s-agent-sandbox-fake`` target, backed by ``FakeKubeApi``."""
    api = FakeKubeApi()
    provider = K8sAgentSandboxProvider(
        _TARGET_NAME,
        {
            "namespace": "spleef-conformance",
            "runtime_class": "gvisor",
            "max_concurrency": 4,
            "ready_timeout_s": 5.0,
            "ready_poll_s": 0.01,
            "enforces_limits": [],
        },
        _EmptySecrets(),
        api=api,
    )

    async def lose_sandbox(sandbox: Sandbox) -> None:
        await provider.lose(sandbox.id)

    async def expire_lifetime() -> None:
        await provider.expire_live_sandboxes()

    async def fail_create(count: int = 1) -> None:
        provider.fail_next_creates(count, SandboxCreateError)

    async def cleanup() -> None:
        await provider.close()

    return [
        ConformanceTarget(
            name=_TARGET_NAME,
            kind="sandbox",
            provider=provider,
            fake=True,
            spec_factory=_spec_factory,
            injectors={
                "lose_sandbox": lose_sandbox,
                "expire_lifetime": expire_lifetime,
                "fail_create": fail_create,
            },
            notes=(
                "in-memory FakeKubeApi: Sandbox CRs are simulated, exec runs local "
                "subprocesses; no Kubernetes cluster, RuntimeClass or Cilium is exercised"
            ),
            cleanup=cleanup,
        )
    ]
