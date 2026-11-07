"""Shared fixtures: a provider pointed at the fake ``container`` executable."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_apple_container.fake_container import install
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

_DIGEST = "sha256:" + "b" * 64


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


@dataclass(frozen=True)
class FakeEnvironment:
    executable: Path
    state: Path
    config: dict[str, Any]


@pytest.fixture
def fake_env(tmp_path: Path) -> FakeEnvironment:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    executable = install(bin_dir, python=sys.executable)
    return FakeEnvironment(
        executable=executable,
        state=state,
        config={
            "container_bin": str(executable),
            "cli_env": {"SPLEEF_FAKE_CONTAINER_ROOT": str(state)},
            "max_concurrency": 4,
            "max_lifetime_s": 7200,
            "boot_timeout_s": 30,
            "ttl_sweep_interval_s": 3600,
        },
    )


def build_spec(
    trial_id: str,
    *,
    network: NetMode = NetMode.OFF,
    max_lifetime_s: int = 600,
    via_proxy: str | None = None,
    allow_hosts: tuple[str, ...] = (),
    allow_cidrs: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref="spleef-test:latest", digest=_DIGEST),
        resources=Resources(cpus=2, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(
            mode=network,
            via_proxy=via_proxy,
            allow_hosts=allow_hosts,
            allow_cidrs=allow_cidrs,
        ),
        max_lifetime_s=max_lifetime_s,
        env=env or {},
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "adapter-test",
        },
    )


@pytest.fixture
def spec_factory() -> Any:
    def factory(trial_id: str, **kwargs: Any) -> SandboxSpec:
        return build_spec(trial_id, **kwargs)

    return factory


@pytest.fixture
async def provider(fake_env: FakeEnvironment) -> AsyncIterator[AppleContainerProvider]:
    instance = AppleContainerProvider("adapter-test", fake_env.config, EmptySecrets())
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def sandbox(provider: AppleContainerProvider, spec_factory: Any) -> AsyncIterator[Any]:
    instance = await provider.create(spec_factory("unit-1"))
    try:
        yield instance
    finally:
        await instance.destroy()


@pytest.fixture
def make_provider(fake_env: FakeEnvironment) -> Iterator[Any]:
    created: list[AppleContainerProvider] = []

    def factory(**overrides: Any) -> AppleContainerProvider:
        config = {**fake_env.config, **overrides}
        instance = AppleContainerProvider("adapter-test", config, EmptySecrets())
        created.append(instance)
        return instance

    yield factory
    for instance in created:
        # Fixture teardown is synchronous; cancel the sweeper tasks it started.
        for task in instance._sweepers.values():
            task.cancel()
