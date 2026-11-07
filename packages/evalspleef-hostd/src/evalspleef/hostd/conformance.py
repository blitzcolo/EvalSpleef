"""Conformance targets: the RemoteProvider against real loopback mTLS gRPC.

``remote-mock``: a daemon in front of the mock sandbox. Everything but the sandbox vendor is
real (TLS handshakes, registration, chunked transfers, error mapping), so the target is marked
``fake`` only because the sandbox behind the daemon is a double.

``remote-docker``: a daemon in front of the docker adapter, discovered through its entry point.
It is built only when that entry point loads and a Docker daemon answers; otherwise the reason
is recorded in ``UNAVAILABLE`` and raised as a warning, never silently dropped.
"""

from __future__ import annotations

import http.client
import os
import platform
import socket
import warnings
from importlib.metadata import entry_points
from pathlib import Path

from evalspleef.conformance.target import ConformanceTarget, SpecFactory
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxProvider,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

from .daemon import NoSecrets
from .loopback import ThreadedLoopbackHostd

UNAVAILABLE: dict[str, str] = {}
"""Targets this installation could not build, with the reason; refreshed by ``build_targets``."""

DOCKER_IMAGE_ENV = "SPLEEF_HOSTD_DOCKER_IMAGE"
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
_DIGEST = "sha256:" + "0" * 64


def _spec_factory(template_ref: str, arch: Arch) -> SpecFactory:
    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=arch,
            template=TemplateRef(kind="oci", ref=template_ref, digest=_DIGEST),
            resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={"conformance": "hostd"},
        )

    return factory


def _remote_mock_target() -> ConformanceTarget:
    clock = FakeClock()
    mock = MockSandboxProvider("mock-sandbox", {}, DictSecretResolver({}), clock=clock)
    harness = ThreadedLoopbackHostd()
    try:
        stack = harness.start({"mock": mock}, adapter="mock-sandbox")
    except BaseException:
        harness.stop()
        raise
    remote = stack.remote("mock")

    async def lose_sandbox(sandbox: Sandbox) -> None:
        mock.lose(sandbox.id)

    async def fail_create(count: int = 1) -> None:
        mock.fail_next_creates(count, SandboxCreateError)

    async def expire_lifetime() -> None:
        clock.advance(10_000)

    async def cleanup() -> None:
        await remote.aclose()
        harness.stop()

    return ConformanceTarget(
        name="remote-mock",
        kind="sandbox",
        provider=remote,
        fake=True,
        spec_factory=_spec_factory("mock-template", Arch.ARM64),
        injectors={
            "lose_sandbox": lose_sandbox,
            "fail_create": fail_create,
            "expire_lifetime": expire_lifetime,
        },
        notes="hostd daemon in front of the mock sandbox over loopback mTLS gRPC; "
        "the sandbox vendor is a double, the transport and security are real",
        cleanup=cleanup,
    )


def _docker_sockets() -> list[str]:
    host = os.environ.get("DOCKER_HOST")
    if host:
        return [host]
    return [
        "unix:///var/run/docker.sock",
        f"unix://{Path.home()}/.docker/run/docker.sock",
    ]


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=3)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(self._path)
        self.sock = sock


def _docker_answers() -> str | None:
    """Return None when a Docker daemon answers /_ping, else the reason it does not."""
    reasons: list[str] = []
    for address in _docker_sockets():
        try:
            connection: http.client.HTTPConnection
            if address.startswith("unix://"):
                connection = _UnixConnection(address.removeprefix("unix://"))
            elif address.startswith("tcp://"):
                host, _, port = address.removeprefix("tcp://").partition(":")
                connection = http.client.HTTPConnection(host, int(port or 2375), timeout=3)
            else:
                reasons.append(f"{address}: unsupported DOCKER_HOST scheme")
                continue
            connection.request("GET", "/_ping")
            if connection.getresponse().status == 200:
                return None
            reasons.append(f"{address}: unexpected /_ping status")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            reasons.append(f"{address}: {exc}")
    return "no Docker daemon answered (" + "; ".join(reasons) + ")"


def _remote_docker_target() -> ConformanceTarget | None:
    matches = list(entry_points(group="evalspleef.sandbox_providers", name="docker"))
    if not matches:
        UNAVAILABLE["remote-docker"] = "the docker adapter entry point is not installed"
        return None
    try:
        provider_class = matches[0].load()
    except ImportError as exc:
        UNAVAILABLE["remote-docker"] = f"the docker adapter entry point does not load: {exc}"
        return None
    reason = _docker_answers()
    if reason is not None:
        UNAVAILABLE["remote-docker"] = reason
        return None
    provider: SandboxProvider = provider_class("hostd-docker", {}, NoSecrets())
    harness = ThreadedLoopbackHostd()
    try:
        stack = harness.start({"docker": provider}, adapter="docker")
    except BaseException:
        harness.stop()
        raise
    remote = stack.remote("docker")
    arch = Arch.ARM64 if platform.machine().lower() in ("arm64", "aarch64") else Arch.X86_64

    async def cleanup() -> None:
        await remote.aclose()
        harness.stop()

    return ConformanceTarget(
        name="remote-docker",
        kind="sandbox",
        provider=remote,
        fake=False,
        spec_factory=_spec_factory(os.environ.get(DOCKER_IMAGE_ENV, DEFAULT_DOCKER_IMAGE), arch),
        notes="hostd daemon on loopback in front of the docker adapter and a local Docker daemon",
        cleanup=cleanup,
    )


def build_targets() -> list[ConformanceTarget]:
    UNAVAILABLE.clear()
    targets = [_remote_mock_target()]
    docker = _remote_docker_target()
    if docker is not None:
        targets.append(docker)
    for name, reason in UNAVAILABLE.items():
        warnings.warn(
            f"conformance target {name} not built: {reason}", RuntimeWarning, stacklevel=2
        )
    return targets
