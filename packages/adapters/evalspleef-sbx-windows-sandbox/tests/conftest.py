"""Shared fixtures: a fake ``wsb`` executable on a temporary PATH and a provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from evalspleef_sbx_windows_sandbox.fake_wsb import install
from evalspleef_sbx_windows_sandbox.provider import WindowsSandboxProvider
from evalspleef_sbx_windows_sandbox.sandbox import WindowsSandbox

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateKind,
    TemplateRef,
)

TEMPLATE_DIGEST = "sha256:" + "0" * 64


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


def build_spec(
    trial_id: str,
    *,
    network: NetworkPolicy | None = None,
    max_lifetime_s: int = 600,
    template_kind: TemplateKind = "vhdx",
    os_family: OSFamily = OSFamily.LINUX,
    label_prefix: str = "adapter-test",
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=os_family,
        arch=Arch.X86_64,
        template=TemplateRef(
            kind=template_kind,
            ref="windows-sandbox-host-image",
            digest=TEMPLATE_DIGEST,
        ),
        resources=Resources(cpus=1, memory_mb=1024, disk_mb=1024),
        network=network or NetworkPolicy(mode=NetMode.OFF),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": label_prefix,
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": label_prefix,
        },
    )


@pytest.fixture
def wsb_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    install(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir / "wsb"


@pytest.fixture
def provider(wsb_path: Path, tmp_path: Path) -> WindowsSandboxProvider:
    return WindowsSandboxProvider(
        "adapter-test",
        {
            "state_dir": str(tmp_path / "state"),
            "guest_os": "linux",
            "exec_timeout_s": 30.0,
        },
        EmptySecrets(),
    )


@pytest.fixture
def spec_factory() -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, **kwargs: object) -> SandboxSpec:
        return build_spec(trial_id, **kwargs)  # type: ignore[arg-type]

    return factory


@pytest.fixture
async def sandbox(
    provider: WindowsSandboxProvider,
    spec_factory: Callable[..., SandboxSpec],
) -> AsyncIterator[WindowsSandbox]:
    instance = await provider.create(spec_factory("unit-1"))
    assert isinstance(instance, WindowsSandbox)
    try:
        yield instance
    finally:
        await instance.destroy()
