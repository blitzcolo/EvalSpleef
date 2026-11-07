"""Conformance target for the Tart provider against a fake ``tart`` executable.

The target is ``tart-fake`` and is always ``fake=True``: it runs the real
provider code, which really spawns ``tart`` processes, but the executable is a
local script that emulates the CLI against directories and host subprocesses.
Nothing here proves behaviour on a real Apple Silicon host with a real macOS
guest.

    pytest -p evalspleef.conformance.plugin --spleef-provider tart-fake \
        packages/adapters/evalspleef-sbx-tart
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from importlib import resources
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

from .provider import TartProvider

_GOLDEN_IMAGE = "ghcr.io/evalspleef/macos-golden"
_GOLDEN_DIGEST = "sha256:" + "a" * 64


class _EmptySecrets:
    """The fake provider needs no secrets; resolve() must never be called."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"the tart provider does not resolve secrets here: {ref!r}")


def install_fake_tart(destination: Path) -> Path:
    """Write the fake CLI from the package to ``destination`` as an executable."""
    script = resources.files("evalspleef_sbx_tart").joinpath("_fake_tart.py").read_text("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(script, encoding="utf-8")
    destination.chmod(0o755)
    return destination


def _spec_factory() -> Callable[..., SandboxSpec]:
    def factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.MACOS,
            arch=Arch.ARM64,
            template=TemplateRef(kind="tart_image", ref=_GOLDEN_IMAGE, digest=_GOLDEN_DIGEST),
            resources=Resources(cpus=2, memory_mb=4096, disk_mb=20480),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={
                "batch": "conformance",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": "tart-fake",
            },
            gui=True,
        )

    return factory


def build_targets() -> list[ConformanceTarget]:
    """The ``tart-fake`` target: the real provider over a fake ``tart`` CLI."""
    root = Path(tempfile.mkdtemp(prefix="spleef-tart-conformance-"))
    bin_dir = root / "bin"
    install_fake_tart(bin_dir / "tart")
    state_dir = root / "state"
    fake_root = root / "fake-tart"
    fake_root.mkdir(parents=True, exist_ok=True)

    previous_path = os.environ.get("PATH", "")
    previous_root = os.environ.get("FAKE_TART_ROOT")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{previous_path}"
    os.environ["FAKE_TART_ROOT"] = str(fake_root)

    provider = TartProvider(
        "tart-fake",
        {
            "tart_bin": "tart",
            "state_dir": str(state_dir),
            "host_macos_slots": 2,
            "start_timeout_s": 30,
            "guest_exec_timeout_s": 300,
            "ttl_sweep_interval_s": 1,
            "vnc_url_timeout_s": 5,
            "exec_transport": "auto",
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
        shutil.rmtree(root, ignore_errors=True)
        os.environ["PATH"] = previous_path
        if previous_root is None:
            os.environ.pop("FAKE_TART_ROOT", None)
        else:
            os.environ["FAKE_TART_ROOT"] = previous_root

    target = ConformanceTarget(
        name="tart-fake",
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=_spec_factory(),
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=(
            "real TartProvider over a fake tart executable on PATH; commands run as host "
            "subprocesses, no Apple Silicon host or macOS guest is involved"
        ),
        cleanup=cleanup,
    )
    return [target]
