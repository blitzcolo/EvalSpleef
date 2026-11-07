"""Conformance target for the Hyper-V provider against a fake host and a fake guest.

The target is ``hyperv-fake`` and is always ``fake=True``: it runs the real
``HyperVProvider`` and the real SSH/SFTP client code, but the Windows host is a
``FakeHostShell`` (records PowerShell scripts, simulates VM state on disk) and
each guest is a local asyncssh server that executes command lines through
``/bin/sh`` and understands a small PowerShell-like subset. Nothing here proves
behaviour on a real Windows Hyper-V host or inside a real Windows guest.

    pytest -p evalspleef.conformance.plugin --spleef-provider hyperv-fake \
        packages/adapters/evalspleef-sbx-hyperv

Fault injectors: ``lose_sandbox`` (the host removes the VM), ``expire_lifetime``
(destroy every live sandbox, mirroring the TTL sweep) and ``fail_create`` (the
next create raises ``SandboxCreateError``).
"""

from __future__ import annotations

import shutil
import socketserver
import tempfile
import threading
from collections.abc import Callable
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

from ._fake_host_shell import FakeHostShell
from .provider import HyperVProvider

GOLDEN_DIGEST = "sha256:" + "a" * 64


class _EmptySecrets:
    """The fake provider needs no secrets; resolve() must never be called."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the hyperv fake target does not resolve secrets: {ref!r}")


class RdpStub:
    """A threaded TCP listener that accepts and closes, standing in for guest RDP.

    It is a plain thread so it outlives the per-case event loops of the kit and
    still answers when the kit reads the GUI endpoint after the sandbox was
    destroyed.
    """

    def __init__(self) -> None:
        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def golden_vhdx(host_root: Path) -> str:
    """Path of the fake golden image under the fake host root."""
    return str(host_root / "golden" / "windows-golden.vhdx")


def make_spec_factory(host_root: Path, *, provider_label: str) -> Callable[..., SandboxSpec]:
    template = TemplateRef(kind="vhdx", ref=golden_vhdx(host_root), digest=GOLDEN_DIGEST)

    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.WINDOWS,
            arch=Arch.X86_64,
            template=template,
            resources=Resources(cpus=2, memory_mb=4096, disk_mb=40960),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={
                "batch": "conformance",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": provider_label,
            },
            gui=True,
        )

    return factory


def build_targets() -> list[ConformanceTarget]:
    """The ``hyperv-fake`` target: the real provider over a fake host and fake guests."""
    root = Path(tempfile.mkdtemp(prefix="spleef-hyperv-conformance-"))
    host_root = root / "host"
    shell = FakeHostShell(host_root)
    rdp = RdpStub()
    provider = HyperVProvider(
        "hyperv-fake",
        {
            "host_shell": shell,
            "state_dir": str(root / "state"),
            "vm_dir": str(host_root / "vhds"),
            "max_concurrency": 4,
            "start_timeout_s": 30,
            "ttl_sweep_interval_s": 1,
            "ssh_user": "agent",
            "ssh_connect_timeout_s": 10,
            "rdp_port": rdp.port,
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
        rdp.close()
        shutil.rmtree(root, ignore_errors=True)

    target = ConformanceTarget(
        name="hyperv-fake",
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=make_spec_factory(host_root, provider_label="hyperv-fake"),
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=(
            "real HyperVProvider over a FakeHostShell (no PowerShell, no Hyper-V) and local "
            "asyncssh guests that run command lines through /bin/sh plus a small PowerShell-like "
            "subset; no Windows host or Windows guest is involved"
        ),
        cleanup=cleanup,
    )
    return [target]
