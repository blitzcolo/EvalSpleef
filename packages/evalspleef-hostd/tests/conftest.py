from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest_asyncio

from evalspleef.hostd.loopback import LoopbackHostd
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

SpecMaker = Callable[..., SandboxSpec]


def make_spec(
    trial_id: str = "t-1", *, max_lifetime_s: int = 600, **overrides: object
) -> SandboxSpec:
    fields: dict[str, object] = {
        "trial_id": trial_id,
        "lease_id": f"lease-{trial_id}",
        "role": "agent",
        "os": OSFamily.LINUX,
        "arch": Arch.ARM64,
        "template": TemplateRef(kind="oci", ref="mock", digest="sha256:" + "0" * 64),
        "resources": Resources(cpus=1, memory_mb=256, disk_mb=512),
        "network": NetworkPolicy(mode=NetMode.OFF),
        "max_lifetime_s": max_lifetime_s,
        "labels": {"suite": "hostd", "trial": trial_id},
    }
    fields.update(overrides)
    return SandboxSpec(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def spec() -> SpecMaker:
    return make_spec


class Stacks:
    """Builds loopback stacks and tears every one of them down."""

    def __init__(self) -> None:
        self.created: list[LoopbackHostd] = []

    async def start(
        self, mock: MockSandboxProvider | None = None, **kwargs: object
    ) -> tuple[LoopbackHostd, MockSandboxProvider]:
        provider = mock or MockSandboxProvider(
            "mock", {}, DictSecretResolver({}), clock=FakeClock()
        )
        stack = await LoopbackHostd.start({"mock": provider}, **kwargs)  # type: ignore[arg-type]
        self.created.append(stack)
        return stack, provider


@pytest_asyncio.fixture
async def stacks() -> AsyncIterator[Stacks]:
    holder = Stacks()
    yield holder
    for stack in holder.created:
        await stack.stop()
