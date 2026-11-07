"""Conformance target for the apple/container provider.

The real CLI needs macOS 26 and Apple silicon, so the shipped target runs the
adapter against the fake ``container`` executable in ``fake_container.py``. The
target is therefore ``fake=True``: a pass here is evidence against a double, not
"verified on a real host".
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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

from .fake_container import install
from .provider import AppleContainerProvider

TARGET_NAME = "apple-container-fake"
_DIGEST = "sha256:" + "a" * 64


@dataclass(frozen=True)
class AppleContainerConformanceTarget(ConformanceTarget):
    """The fake-backed target; adds the canary host the network cases look for."""

    reachable_from_sandbox: str = ""


class _EmptySecrets:
    """The provider needs no secrets; resolve() must never be called."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the apple-container provider does not resolve secrets: {ref!r}")


def _spec_factory(target_name: str) -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.ARM64,
            template=TemplateRef(kind="oci", ref="spleef-conformance:latest", digest=_DIGEST),
            resources=Resources(cpus=2, memory_mb=1024, disk_mb=2048),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={
                "batch": "conformance",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": target_name,
            },
        )

    return factory


def build_targets() -> list[ConformanceTarget]:
    """The ``apple-container-fake`` target against the fake CLI on a temp PATH."""
    directory = Path(tempfile.mkdtemp(prefix="spleef-apple-container-fake-"))
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    executable = install(bin_dir, python=sys.executable)
    state = directory / "state"
    state.mkdir()
    provider = AppleContainerProvider(
        TARGET_NAME,
        {
            "container_bin": str(executable),
            "cli_env": {"SPLEEF_FAKE_CONTAINER_ROOT": str(state)},
            "max_concurrency": 4,
            "max_lifetime_s": 7200,
            "boot_timeout_s": 30,
            "ttl_sweep_interval_s": 3600,
        },
        _EmptySecrets(),
    )

    async def lose_sandbox(sandbox: Sandbox) -> None:
        await provider.lose(sandbox.id)

    async def expire_lifetime() -> None:
        await provider.expire_live_sandboxes()

    async def fail_create(count: int = 1) -> None:
        provider.fail_next_creates(count, SandboxCreateError)

    async def cleanup() -> None:
        await provider.close()
        shutil.rmtree(directory, ignore_errors=True)

    target = AppleContainerConformanceTarget(
        name=TARGET_NAME,
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=_spec_factory(TARGET_NAME),
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=(
            "fake container CLI backed by local directories; every VM, network and "
            "resource limit is simulated on this host"
        ),
        cleanup=cleanup,
        reachable_from_sandbox="127.0.0.1",
    )
    return [target]
