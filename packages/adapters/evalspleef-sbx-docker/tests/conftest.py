"""Shared fixtures for the docker adapter tests (real local daemon)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from evalspleef_sbx_docker.conformance import _ensure_test_image
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _require_docker() -> None:
    if not docker_available():
        pytest.skip("no working local Docker daemon")


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


def build_spec(
    trial_id: str,
    image_id: str,
    *,
    network: NetMode = NetMode.OFF,
    max_lifetime_s: int = 600,
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref="spleef-test-sandbox:local", digest=image_id),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "adapter-test",
        },
    )


@pytest.fixture(scope="module")
def test_image_id() -> str:
    _require_docker()
    image_id, _ = _ensure_test_image()
    return image_id


@pytest.fixture
def spec_factory(test_image_id: str) -> Any:
    def factory(trial_id: str, **kwargs: Any) -> SandboxSpec:
        return build_spec(trial_id, test_image_id, **kwargs)

    return factory


@pytest.fixture
async def provider() -> Iterator[DockerProvider]:
    _require_docker()
    instance = DockerProvider(
        "adapter-test",
        {"max_concurrency": 4, "pids_limit": 64, "ttl_sweep_interval_s": 1},
        EmptySecrets(),
    )
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def sandbox(provider: DockerProvider, spec_factory: Any) -> Iterator[Any]:
    instance = await provider.create(spec_factory("unit-1"))
    try:
        yield instance
    finally:
        await instance.destroy()
