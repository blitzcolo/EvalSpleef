"""Shared fixtures for the k8s agent-sandbox adapter tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from evalspleef_sbx_k8s_agent_sandbox.fake import FakeKubeApi
from evalspleef_sbx_k8s_agent_sandbox.provider import K8sAgentSandboxProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

TEMPLATE_REF = "ghcr.io/evalspleef/test-sandbox:latest"
TEMPLATE_DIGEST = "sha256:" + "0" * 64


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_spec(
    trial_id: str,
    *,
    network: NetMode = NetMode.OFF,
    max_lifetime_s: int = 600,
    **overrides: Any,
) -> SandboxSpec:
    spec = SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref=TEMPLATE_REF, digest=TEMPLATE_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=10240),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "adapter-test",
        },
    )
    return replace(spec, **overrides)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def api(clock: Clock) -> FakeKubeApi:
    return FakeKubeApi(clock=clock)


@pytest.fixture
def provider(api: FakeKubeApi, clock: Clock) -> K8sAgentSandboxProvider:
    return K8sAgentSandboxProvider(
        "adapter-test",
        {
            "namespace": "spleef-test",
            "runtime_class": "gvisor",
            "max_concurrency": 8,
            "ready_timeout_s": 2.0,
            "ready_poll_s": 0.01,
        },
        EmptySecrets(),
        api=api,
        clock=clock,
    )


@pytest.fixture
async def sandbox(provider: K8sAgentSandboxProvider):
    instance = await provider.create(build_spec("unit-1"))
    try:
        yield instance
    finally:
        await instance.destroy()
