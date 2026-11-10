"""Conformance targets for the apple/container provider.

``apple-container-fake`` always exists: the adapter against the fake ``container``
executable in ``fake_container.py`` (``fake=True``, evidence against a double).

``apple-container-live`` runs the adapter against the real ``container`` CLI. It
is built only when the ``container`` executable is on ``PATH`` **and**
``SPLEEF_APPLE_CONTAINER_IMAGE`` names a locally pulled OCI image, so a plain
``pytest`` on another machine never creates a VM. Optional knobs:

``SPLEEF_APPLE_CONTAINER_USER``
    account the sandboxes run as; it must exist in the image (default ``nobody``).
``SPLEEF_APPLE_CONTAINER_NAME_PREFIX``
    container-name prefix (default ``spleef-conformance-``). The name is also the
    guest hostname.

The fault injectors drive the CLI in both targets: a lost sandbox is removed out
of band with ``container delete --force``; a create failure is a real ``run``
failing because an injector-created container already holds the pinned name;
lifetime expiry runs the adapter's own TTL sweep with the clock past the lifetime.

``observer_hosts`` lists the IPv4 gateway of every network the CLI reports when
C-NET-01 asks (after its sandbox exists, so an internal network the adapter created
on demand is included): from an ``off`` sandbox the host must be unreachable through
every one of them, not only through the ``default`` network's gateway.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import SpleefError
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
LIVE_TARGET_NAME = "apple-container-live"
LIVE_IMAGE_ENV = "SPLEEF_APPLE_CONTAINER_IMAGE"
LIVE_USER_ENV = "SPLEEF_APPLE_CONTAINER_USER"
LIVE_PREFIX_ENV = "SPLEEF_APPLE_CONTAINER_NAME_PREFIX"
_DIGEST = "sha256:" + "a" * 64
_FAKE_IMAGE = "spleef-conformance:latest"
_CLI_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class AppleContainerConformanceTarget(ConformanceTarget):
    """A target that also names the canary host the network cases look for."""

    reachable_from_sandbox: str = ""


class _EmptySecrets:
    """The provider needs no secrets; resolve() must never be called."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the apple-container provider does not resolve secrets: {ref!r}")


def _spec_factory(
    target_name: str, template: TemplateRef, run_as_user: str
) -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.ARM64,
            template=template,
            resources=Resources(cpus=2, memory_mb=1024, disk_mb=2048),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            run_as_user=run_as_user,
            labels={
                "batch": "conformance",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": target_name,
            },
        )

    return factory


def _build_target(
    name: str,
    provider: AppleContainerProvider,
    *,
    template: TemplateRef,
    run_as_user: str,
    conflict_prefix: str,
    fake: bool,
    notes: str,
    reachable_from_sandbox: str,
    extra_cleanup: Callable[[], None] | None = None,
) -> AppleContainerConformanceTarget:
    image = template.ref
    occupied: list[str] = []

    async def cli(*args: str) -> None:
        result = await provider.cli.run(list(args), timeout_s=_CLI_TIMEOUT_S)
        if result.exit_code != 0:
            raise SpleefError(
                f"container {' '.join(args[:2])} failed: {result.stderr_text.strip()[:400]}"
            )

    async def lose_sandbox(sandbox: Sandbox) -> None:
        # The carrier losing the VM, out of band of the adapter's own bookkeeping.
        await cli("delete", "--force", sandbox.id)

    async def fail_create(count: int = 1) -> None:
        for _ in range(count):
            taken = f"{conflict_prefix}{secrets.token_hex(6)}"
            await cli("create", "--name", taken, image, "sleep", "infinity")
            occupied.append(taken)
            provider.pin_next_sandbox_id(taken)

    async def observer_hosts() -> list[str]:
        result = await provider.cli.run(
            ["network", "list", "--format", "json"], timeout_s=_CLI_TIMEOUT_S
        )
        if result.exit_code != 0:
            raise SpleefError(f"container network list failed: {result.stderr_text[:400]}")
        return _network_gateways(json.loads(result.stdout_text))

    async def expire_lifetime() -> None:
        lifetime = provider.capabilities().max_lifetime_s or 0
        await provider.sweep_expired(now=time.time() + lifetime + 1)

    async def cleanup() -> None:
        try:
            await provider.reap({"batch": "conformance", "provider": name}, older_than_s=0)
            for taken in occupied:
                await provider.release_sandbox_id(taken)
        finally:
            await provider.close()
            if extra_cleanup is not None:
                extra_cleanup()

    return AppleContainerConformanceTarget(
        name=name,
        kind="sandbox",
        provider=provider,
        fake=fake,
        spec_factory=_spec_factory(name, template, run_as_user),
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=notes,
        cleanup=cleanup,
        observer_hosts=observer_hosts,
        reachable_from_sandbox=reachable_from_sandbox,
    )


def _fake_target() -> AppleContainerConformanceTarget:
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
    return _build_target(
        TARGET_NAME,
        provider,
        template=TemplateRef(kind="oci", ref=_FAKE_IMAGE, digest=_DIGEST),
        run_as_user="agent",
        conflict_prefix="c-taken-",
        fake=True,
        notes=(
            "fake container CLI backed by local directories; every VM, network and "
            "resource limit is simulated on this host"
        ),
        reachable_from_sandbox="127.0.0.1",
        extra_cleanup=lambda: shutil.rmtree(directory, ignore_errors=True),
    )


def _cli_json(executable: str, *args: str) -> object:
    completed = subprocess.run(
        [executable, *args], capture_output=True, text=True, timeout=_CLI_TIMEOUT_S, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"'container {' '.join(args)}' failed ({completed.returncode}): "
            f"{completed.stderr.strip()[:400]}"
        )
    return json.loads(completed.stdout)


def _image_digest(executable: str, image: str) -> str:
    """The digest the local image store holds for ``image``; the sandboxes pin to it."""
    try:
        entries = _cli_json(executable, "image", "inspect", image)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{LIVE_IMAGE_ENV}={image!r} is not in the local image store; "
            f"pull it first with 'container image pull {image}' ({exc})"
        ) from exc
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        descriptor = entries[0].get("configuration", {}).get("descriptor", {})
        digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
        if isinstance(digest, str) and digest.startswith("sha256:"):
            return digest
    raise RuntimeError(f"'container image inspect {image}' reported no sha256 digest")


def _network_gateways(entries: object, network: str | None = None) -> list[str]:
    """IPv4 gateways from ``container network list --format json``; one network if named."""
    gateways: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or network not in (None, entry.get("id")):
            continue
        status = entry.get("status")
        gateway = status.get("ipv4Gateway") if isinstance(status, dict) else None
        if isinstance(gateway, str) and gateway:
            gateways.append(gateway)
    return gateways


def _host_gateway(executable: str, network: str) -> str:
    """The host address as seen from a sandbox on ``network`` (its IPv4 gateway)."""
    entries = _cli_json(executable, "network", "list", "--format", "json")
    gateways = _network_gateways(entries, network)
    if gateways:
        return gateways[0]
    raise RuntimeError(f"network {network!r} reports no IPv4 gateway")


def _live_target(executable: str, image: str) -> AppleContainerConformanceTarget:
    run_as_user = os.environ.get(LIVE_USER_ENV) or "nobody"
    prefix = os.environ.get(LIVE_PREFIX_ENV) or "spleef-conformance-"
    digest = _image_digest(executable, image)
    provider = AppleContainerProvider(
        LIVE_TARGET_NAME,
        {
            "container_bin": executable,
            "name_prefix": prefix,
            "internal_network": f"{prefix}internal",
            # A small declared limit keeps C-CONC-01 to a handful of VMs.
            "max_concurrency": 3,
            "max_lifetime_s": 7200,
            "boot_timeout_s": 120,
            "ttl_sweep_interval_s": 3600,
        },
        _EmptySecrets(),
    )
    return _build_target(
        LIVE_TARGET_NAME,
        provider,
        template=TemplateRef(kind="oci", ref=image, digest=digest),
        run_as_user=run_as_user,
        conflict_prefix=f"{prefix}taken-",
        fake=False,
        notes=f"real Apple container CLI: one lightweight Linux VM per sandbox from {image}",
        reachable_from_sandbox=_host_gateway(executable, "default"),
    )


def build_targets() -> list[ConformanceTarget]:
    """The fake target, plus ``apple-container-live`` when the CLI and an image are configured."""
    targets: list[ConformanceTarget] = [_fake_target()]
    image = os.environ.get(LIVE_IMAGE_ENV)
    executable = shutil.which("container")
    if image and executable:
        targets.append(_live_target(executable, image))
    return targets
