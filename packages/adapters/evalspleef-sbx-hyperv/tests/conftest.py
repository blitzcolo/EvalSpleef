"""Fixtures for the hyperv adapter tests.

The provider under test is the real ``HyperVProvider``. The Windows host is a
``FakeHostShell`` (records PowerShell scripts, simulates VM state on disk) and
every started VM is served by a local asyncssh "guest" that runs command lines
through ``/bin/sh`` plus a small PowerShell-like subset. Nothing here talks to
Hyper-V or to a Windows guest: all of it is "not verified on a real host".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from evalspleef_sbx_hyperv._fake_host_shell import FakeHostShell
from evalspleef_sbx_hyperv.provider import HyperVProvider
from evalspleef_sbx_hyperv.sandbox import HyperVSandbox

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef

GOLDEN_DIGEST = "sha256:" + "a" * 64

ProviderFactory = Callable[..., HyperVProvider]
SpecFactory = Callable[..., SandboxSpec]


class DictSecrets:
    """A secret resolver backed by a dict; unknown references fail loudly."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def resolve(self, ref: str) -> str:
        if ref not in self._values:
            raise RuntimeError(f"unexpected secret resolution: {ref!r}")
        return self._values[ref]


def golden_path(host_root: Path) -> str:
    return str(host_root / "golden" / "windows-golden.vhdx")


def build_spec(
    host_root: Path,
    trial_id: str,
    *,
    os: OSFamily = OSFamily.WINDOWS,
    arch: Arch = Arch.X86_64,
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
        template=TemplateRef(kind="vhdx", ref=golden_path(host_root), digest=GOLDEN_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=40960),
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
            "provider": "test-hyperv",
        },
        max_lifetime_s=max_lifetime_s,
        gui=gui,
    )


@pytest.fixture
def host_root(tmp_path: Path) -> Path:
    return tmp_path / "host"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def spec_factory(host_root: Path) -> SpecFactory:
    def factory(trial_id: str, **kwargs: Any) -> SandboxSpec:
        return build_spec(host_root, trial_id, **kwargs)

    return factory


@pytest.fixture
async def fake_shell(host_root: Path) -> AsyncIterator[FakeHostShell]:
    shell = FakeHostShell(host_root)
    try:
        yield shell
    finally:
        await shell.aclose()


@pytest.fixture
async def provider_factory(
    fake_shell: FakeHostShell, host_root: Path, state_dir: Path
) -> AsyncIterator[ProviderFactory]:
    instances: list[HyperVProvider] = []

    def factory(*, secrets: DictSecrets | None = None, **overrides: Any) -> HyperVProvider:
        config: dict[str, Any] = {
            "host_shell": fake_shell,
            "state_dir": str(state_dir),
            "vm_dir": str(host_root / "vhds"),
            "max_concurrency": 4,
            "start_timeout_s": 30,
            "ttl_sweep_interval_s": 3600,
            "ssh_user": "agent",
            "ssh_connect_timeout_s": 10,
            "rdp_port": 3390,
        }
        config.update(overrides)
        provider = HyperVProvider("test-hyperv", config, secrets or DictSecrets())
        instances.append(provider)
        return provider

    try:
        yield factory
    finally:
        for provider in instances:
            await provider.close()


@pytest.fixture
async def provider(provider_factory: ProviderFactory) -> HyperVProvider:
    return provider_factory()


async def _create(provider: HyperVProvider, spec: SandboxSpec) -> HyperVSandbox:
    instance = await provider.create(spec)
    assert isinstance(instance, HyperVSandbox)
    return instance


@pytest.fixture
async def sandbox(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> AsyncIterator[HyperVSandbox]:
    instance = await _create(provider, spec_factory("unit-1"))
    try:
        yield instance
    finally:
        await instance.destroy()


@pytest.fixture
async def linux_sandbox(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> AsyncIterator[HyperVSandbox]:
    instance = await _create(provider, spec_factory("unit-linux", os=OSFamily.LINUX))
    try:
        yield instance
    finally:
        await instance.destroy()
