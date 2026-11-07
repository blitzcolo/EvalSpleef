"""Fixtures for the tart adapter tests: a real provider over a fake ``tart`` CLI."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_tart.conformance import install_fake_tart
from evalspleef_sbx_tart.provider import TartProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

GOLDEN_IMAGE = "ghcr.io/evalspleef/macos-golden"
GOLDEN_DIGEST = "sha256:" + "a" * 64

ProviderFactory = Callable[..., TartProvider]


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


@pytest.fixture(scope="session")
def fake_tart_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bin_dir = tmp_path_factory.mktemp("tart-bin")
    return install_fake_tart(bin_dir / "tart")


@pytest.fixture
def fake_tart_root(tmp_path: Path, fake_tart_bin: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "fake-tart"
    root.mkdir()
    monkeypatch.setenv("FAKE_TART_ROOT", str(root))
    monkeypatch.setenv("PATH", f"{fake_tart_bin.parent}{os.pathsep}{os.environ['PATH']}")
    return root


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def build_spec(
    trial_id: str,
    *,
    os: OSFamily = OSFamily.MACOS,
    arch: Arch = Arch.ARM64,
    network: NetMode = NetMode.OFF,
    allow_hosts: tuple[str, ...] = (),
    allow_cidrs: tuple[str, ...] = (),
    via_proxy: str | None = None,
    max_lifetime_s: int = 600,
    gui: bool = False,
    env: dict[str, str] | None = None,
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=os,
        arch=arch,
        template=TemplateRef(kind="tart_image", ref=GOLDEN_IMAGE, digest=GOLDEN_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=20480),
        network=NetworkPolicy(
            mode=network,
            allow_hosts=allow_hosts,
            allow_cidrs=allow_cidrs,
            via_proxy=via_proxy,
        ),
        run_as_user="agent",
        env=env or {},
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "test-tart",
        },
        max_lifetime_s=max_lifetime_s,
        gui=gui,
    )


@pytest.fixture
def spec_factory() -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, **kwargs: Any) -> SandboxSpec:
        return build_spec(trial_id, **kwargs)

    return factory


@pytest.fixture
async def provider_factory(fake_tart_root: Path, state_dir: Path) -> AsyncIterator[ProviderFactory]:
    instances: list[TartProvider] = []

    def factory(**overrides: Any) -> TartProvider:
        config: dict[str, Any] = {
            "tart_bin": "tart",
            "state_dir": str(state_dir),
            "host_macos_slots": 2,
            "start_timeout_s": 30,
            "guest_exec_timeout_s": 120,
            "ttl_sweep_interval_s": 1,
            "vnc_url_timeout_s": 5,
            "exec_transport": "auto",
        }
        config.update(overrides)
        provider = TartProvider("test-tart", config, EmptySecrets())
        instances.append(provider)
        return provider

    try:
        yield factory
    finally:
        for provider in instances:
            await provider.close()


@pytest.fixture
async def provider(provider_factory: ProviderFactory) -> TartProvider:
    return provider_factory()


@pytest.fixture
async def sandbox(provider: TartProvider, spec_factory: Callable[..., SandboxSpec]) -> Any:
    instance = await provider.create(spec_factory("unit-1"))
    try:
        yield instance
    finally:
        await instance.destroy()
