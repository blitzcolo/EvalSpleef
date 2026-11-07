"""Conformance target for the Windows Sandbox adapter, against a fake ``wsb``.

Windows Sandbox only exists on Windows, so the target is always ``fake=True``:
a Python script installed on ``PATH`` as ``wsb`` emulates ``start``/``exec``/
``stop`` over a local directory and the conformance kit exercises the adapter
mechanics (lifecycle, labels, exec, timeouts, transfers, fault injectors)
against it. Nothing here is verified on a real Windows host.

The target configures the provider with ``guest_os="linux"``: the fake executes
the command line with ``/bin/sh`` on the host, so the kit's POSIX command
variants are the ones that can run. A real deployment keeps the default
``guest_os="windows"``; this switch only exists for the fake harness and the
README records it as such.

    pytest -p evalspleef.conformance.plugin --spleef-provider windows-sandbox-fake \
        --spleef-report /tmp/spleef-windows-sandbox-fake-report.json \
        packages/adapters/evalspleef-sbx-windows-sandbox -q
"""

from __future__ import annotations

import os
import shutil
import tempfile
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

from .fake_wsb import install
from .provider import WindowsSandboxProvider

TARGET_NAME = "windows-sandbox-fake"
_TEMPLATE_DIGEST = "sha256:" + "0" * 64


class _EmptySecrets:
    """The provider resolves no secrets; a resolve call is a bug."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the windows-sandbox provider resolves no secrets: {ref!r}")


def _install_fake_wsb() -> tuple[Path, Path]:
    """Install the fake ``wsb`` under a fresh temp root and put it on ``PATH``."""
    root = Path(tempfile.mkdtemp(prefix="spleef-wsb-conformance-"))
    bin_dir = root / "bin"
    executable = install(bin_dir)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return root, executable


def _spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(
            kind="vhdx", ref="windows-sandbox-host-image", digest=_TEMPLATE_DIGEST
        ),
        resources=Resources(cpus=1, memory_mb=1024, disk_mb=1024),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": "conformance",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": TARGET_NAME,
        },
    )


def build_targets() -> list[ConformanceTarget]:
    """The ``windows-sandbox-fake`` target: the adapter over a fake ``wsb``."""
    root, executable = _install_fake_wsb()
    state_dir = root / "state"
    provider = WindowsSandboxProvider(
        TARGET_NAME,
        {
            # The executable is also installed on PATH; the absolute path keeps the
            # target working when another target cleans up its own PATH entry.
            "wsb_path": str(executable),
            "state_dir": str(state_dir),
            "guest_os": "linux",
            "max_lifetime_s": 1800,
            "exec_timeout_s": 120.0,
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
        await _cleanup(provider, root)

    return [
        ConformanceTarget(
            name=TARGET_NAME,
            kind="sandbox",
            provider=provider,
            fake=True,
            spec_factory=_spec_factory,
            injectors={
                "lose_sandbox": lose_sandbox,
                "expire_lifetime": expire_lifetime,
                "fail_create": fail_create,
            },
            notes=(
                "fake wsb executable on a temporary PATH running /bin/sh on this host; "
                "guest_os=linux and fake=True, so nothing here is verified on a real "
                "Windows host; the adapter is dev_only and never routed in a formal batch"
            ),
            cleanup=cleanup,
        )
    ]


async def _cleanup(provider: WindowsSandboxProvider, root: Path) -> None:
    """Stop every instance of this target, delete its temp root and PATH entry."""
    try:
        for entry in await provider.list({}):
            await provider.release_sandbox(str(entry["id"]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        bin_dir = str(root / "bin")
        entries = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part != bin_dir]
        os.environ["PATH"] = os.pathsep.join(entries)


__all__ = ["TARGET_NAME", "build_targets"]
