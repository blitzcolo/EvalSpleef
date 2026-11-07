"""Shared fixtures for the runner tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.runner import RunnerBudget
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.spi.secrets import SecretRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider

DIGEST = "sha256:" + "0" * 64


class FakeSecrets:
    def resolve(self, ref: SecretRef | str) -> str:
        return f"secret:{ref}"


def make_budget(**overrides: Any) -> RunnerBudget:
    values: dict[str, Any] = {
        "max_total_tokens": 1_000_000,
        "max_cost_usd": 100.0,
        "max_wall_clock_s": 3600,
    }
    values.update(overrides)
    return RunnerBudget(**values)


@pytest.fixture
async def sandbox() -> AsyncIterator[MockSandbox]:
    provider = MockSandboxProvider("mock", {}, FakeSecrets(), clock=FakeClock())
    spec = SandboxSpec(
        trial_id="t1",
        lease_id="l1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref="mock:latest", digest=DIGEST),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=NetMode.OFF),
        labels={"batch": "b1", "trial": "t1"},
        max_lifetime_s=3600,
    )
    result = await provider.create(spec)
    assert isinstance(result, MockSandbox)
    yield result
    await result.destroy()


@pytest.fixture
def budget() -> RunnerBudget:
    return make_budget()


@pytest.fixture
def anyio_backend() -> Iterator[str]:
    # pytest-asyncio is configured in auto mode at the repo root; this fixture is
    # only here to make the intent explicit for readers.
    yield "asyncio"
