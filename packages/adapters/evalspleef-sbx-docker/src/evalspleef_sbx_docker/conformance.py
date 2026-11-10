"""Conformance target for the local Docker daemon.

Builds (or reuses) a small unprivileged test image derived from
``python:3.12-slim`` and exposes the ``local-docker`` target so the conformance
kit can run every applicable case against the real daemon:

    pytest -p evalspleef.conformance.plugin --spleef-provider local-docker \
        packages/adapters/evalspleef-sbx-docker

Fault injectors: ``lose_sandbox`` (``docker rm -f`` the container),
``expire_lifetime`` (destroy every live sandbox, mirroring the TTL sweep) and
``fail_create`` (the next create raises ``SandboxCreateError``).

The target sets ``reachable_from_sandbox`` to the host gateway address so the
network canary cases can reach an HTTP observer started on the host, and
``observer_hosts`` to the host alias plus the IPv4 gateway of every Docker network,
which C-NET-01 must all find unreachable from an ``off`` sandbox.
"""

from __future__ import annotations

import asyncio
import ipaddress
import subprocess
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

from .provider import DockerProvider

_TEST_IMAGE = "spleef-test-sandbox:local"
_BASE_IMAGE = "python:3.12-slim"
_DOCKER_BIN = "docker"

_DOCKERFILE = f"""\
FROM {_BASE_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends util-linux procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 --shell /bin/sh agent \
 && mkdir -p /workspace \
 && chown -R agent:agent /workspace /home/agent
USER agent
"""


@dataclass(frozen=True)
class DockerConformanceTarget(ConformanceTarget):
    """``local-docker`` target; adds the canary host the network cases look for."""

    reachable_from_sandbox: str = ""


class _EmptySecrets:
    """The docker provider needs no secrets; resolve() must never be called."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the docker provider does not resolve secrets: {ref!r}")


def _run_docker(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        [_DOCKER_BIN, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join([_DOCKER_BIN, *args])} failed ({result.returncode}): "
            f"{result.stderr.strip()[:400]}"
        )
    return result.stdout


def _image_present(reference: str) -> bool:
    result = subprocess.run(
        [_DOCKER_BIN, "images", "-q", reference],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _ensure_test_image() -> tuple[str, bool]:
    """Build the unprivileged test image if absent; returns (image id, pulled_base)."""
    pulled_base = False
    if not _image_present(_TEST_IMAGE):
        if not _image_present(_BASE_IMAGE):
            _run_docker(["pull", _BASE_IMAGE])
            pulled_base = True
        with tempfile.TemporaryDirectory(prefix="spleef-docker-test-image-") as tmp:
            Path(tmp, "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
            _run_docker(["build", "--quiet", "-t", _TEST_IMAGE, tmp])
    image_id = _run_docker(["image", "inspect", _TEST_IMAGE, "-f", "{{.Id}}"]).strip()
    if not image_id:
        raise RuntimeError(f"docker did not report an id for {_TEST_IMAGE}")
    return image_id, pulled_base


def _host_gateway() -> str:
    """The address at which containers reach services bound on the host."""
    try:
        gateway = _run_docker(
            ["network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"]
        ).strip()
        if gateway:
            return gateway
    except RuntimeError:
        pass
    return "host.docker.internal"


def _observer_hosts() -> list[str]:
    """The host alias and the IPv4 gateway of every network the daemon knows."""
    networks = _run_docker(["network", "ls", "-q"]).split()
    template = "{{range .IPAM.Config}}{{.Gateway}} {{end}}"
    gateways = _run_docker(["network", "inspect", "-f", template, *networks]).split()
    ipv4 = [gw for gw in gateways if isinstance(ipaddress.ip_address(gw), ipaddress.IPv4Address)]
    return ["host.docker.internal", *ipv4]


async def _observer_hosts_async() -> list[str]:
    return await asyncio.to_thread(_observer_hosts)


def _spec_factory(image_id: str) -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.ARM64,
            template=TemplateRef(kind="oci", ref=_TEST_IMAGE, digest=image_id),
            resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={
                "batch": "conformance",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": "local-docker",
            },
        )

    return factory


def build_targets() -> list[ConformanceTarget]:
    """The ``local-docker`` target against the real local Docker daemon."""
    image_id, pulled_base = _ensure_test_image()
    provider = DockerProvider(
        "local-docker",
        {
            "max_concurrency": 8,
            "max_lifetime_s": 7200,
            "pids_limit": 64,
            "ttl_sweep_interval_s": 1,
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
        await _cleanup(provider, pulled_base)

    target = DockerConformanceTarget(
        name="local-docker",
        kind="sandbox",
        provider=provider,
        fake=False,
        spec_factory=_spec_factory(image_id),
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=(
            "real local Docker daemon; test image spleef-test-sandbox:local built from "
            "python:3.12-slim with an unprivileged 'agent' user, /workspace and no sudo"
        ),
        cleanup=cleanup,
        observer_hosts=_observer_hosts_async,
        reachable_from_sandbox=_host_gateway(),
    )
    return [target]


async def _cleanup(provider: DockerProvider, pulled_base: bool) -> None:
    """Remove what this target created: containers, the test image and (if we
    pulled it) the base image; then release the provider's clients."""
    try:
        entries = await provider.list({"provider": "local-docker"})
    except Exception:
        entries = []
    for entry in entries:
        try:
            await provider.client().containers.container(entry["id"]).delete(force=True)
        except Exception:
            continue
    await provider.close()
    if _image_present(_TEST_IMAGE):
        _run_docker(["rmi", "-f", _TEST_IMAGE])
    if pulled_base and _image_present(_BASE_IMAGE):
        _run_docker(["rmi", "-f", _BASE_IMAGE])
