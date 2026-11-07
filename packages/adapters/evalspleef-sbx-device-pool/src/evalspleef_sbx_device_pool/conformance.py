"""Conformance targets for evalspleef-sbx-device-pool.

Exposes ``device-pool-fake`` and ``perf-host-fake`` targets under the
``evalspleef.conformance_targets`` entry-point group.

These targets run against fake hdc executables and fake system probes,
setting ``fake=True`` to clearly record that they are not verified on a real host.
Injectors:
- lose_sandbox: marks sandbox lost or removes device
- fail_create: next create raises SandboxCreateError
- expire_lifetime: advances expiration check or destroys expired sandboxes
"""

from __future__ import annotations

import shutil
import stat
import tempfile
import time
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

from .perf import FakeSystemProbe, PerfHostProvider
from .provider import DevicePoolProvider


class _NoSecrets:
    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"device-pool provider does not resolve secrets: {ref!r}")


def _create_fake_hdc_script(dir_path: Path) -> Path:
    """Create a python script acting as fake hdc binary."""
    hdc_script = dir_path / "hdc"
    script_content = """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
# Skip target flag if present
if len(args) >= 2 and args[0] == "-t":
    serial = args[1]
    args = args[2:]

if not args:
    print("hdc fake CLI")
    sys.exit(0)

cmd = args[0]
if cmd == "list" and args[1:] == ["targets"]:
    print("Hate-AL00-001\\nHate-AL00-002")
    sys.exit(0)

if cmd == "install":
    print("Success")
    sys.exit(0)

if cmd == "uninstall":
    print("Success")
    sys.exit(0)

if cmd == "shell":
    sub = args[1:]
    if sub and sub[0] == "aa":
        print("Ability started/stopped")
        sys.exit(0)
    if sub and sub[0] == "bm" and len(sub) >= 2 and sub[1] == "dump":
        print("com.huawei.system\\ncom.ohos.settings")
        sys.exit(0)
    if sub and sub[0] == "rm":
        sys.exit(0)
    if sub and sub[0] == "reboot":
        print("Rebooting device...")
        sys.exit(0)
    if sub and sub[0] == "hilog":
        print("11-07 12:00:00.000 App info: test hilog")
        sys.exit(0)
    if sub and sub[0] == "snapshot_display":
        sys.exit(0)
    print("shell executed")
    sys.exit(0)

sys.exit(0)
"""
    hdc_script.write_text(script_content, encoding="utf-8")
    hdc_script.chmod(hdc_script.stat().st_mode | stat.S_IEXEC)
    return hdc_script


def _spec_factory_device_pool(
    trial_id: str,
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
        template=TemplateRef(
            kind="device_profile",
            ref="harmony-build-container",
            digest="sha256:" + "a" * 64,
        ),
        resources=Resources(cpus=4, memory_mb=8192, disk_mb=32768),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={"conformance": "device-pool-fake", "trial_id": trial_id},
    )


def _spec_factory_perf_host(
    trial_id: str,
    *,
    network: NetMode = NetMode.OFF,
    max_lifetime_s: int = 600,
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.MACOS,
        arch=Arch.ARM64,
        template=TemplateRef(
            kind="device_profile",
            ref="macos-metal-host",
            digest="sha256:" + "b" * 64,
        ),
        resources=Resources(cpus=8, memory_mb=16384, disk_mb=65536, gpus=1),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={"conformance": "perf-host-fake", "trial_id": trial_id},
    )


def build_targets() -> list[ConformanceTarget]:
    """Exposes conformance targets for the device-pool and perf-host sandbox providers."""
    temp_dir = Path(tempfile.mkdtemp(prefix="spleef_conformance_devpool_"))
    hdc_bin = _create_fake_hdc_script(temp_dir)

    devices_config = [
        {
            "serial": "Hate-AL00-001",
            "model": "Hate-AL00",
            "os_version": "HarmonyOS 6.0.0",
            "state": "free",
            "baseline_packages": ["com.huawei.system", "com.ohos.settings"],
        },
        {
            "serial": "Hate-AL00-002",
            "model": "Hate-AL00",
            "os_version": "HarmonyOS 6.0.0",
            "state": "free",
            "baseline_packages": ["com.huawei.system", "com.ohos.settings"],
        },
    ]

    dev_provider = DevicePoolProvider(
        instance="device-pool-fake",
        config={
            "devices": devices_config,
            "hdc_path": str(hdc_bin),
        },
        secrets=_NoSecrets(),
    )

    async def lose_sandbox(sandbox: Sandbox) -> None:
        if hasattr(sandbox, "mark_lost"):
            sandbox.mark_lost()

    async def fail_create(count: int = 1) -> None:
        dev_provider.fail_next_creates(count, SandboxCreateError)

    async def expire_lifetime() -> None:
        for sbx in list(dev_provider._active_sandboxes.values()):
            sbx.created_at = time.time() - sbx.spec.max_lifetime_s - 10

    async def cleanup() -> None:
        shutil.rmtree(temp_dir, ignore_errors=True)

    target_dev = ConformanceTarget(
        name="device-pool-fake",
        kind="sandbox",
        provider=dev_provider,
        fake=True,
        spec_factory=_spec_factory_device_pool,
        injectors={
            "lose_sandbox": lose_sandbox,
            "fail_create": fail_create,
            "expire_lifetime": expire_lifetime,
        },
        notes="HarmonyOS device pool running against a fake hdc CLI; not verified on a real host",
        cleanup=cleanup,
    )

    fake_probe = FakeSystemProbe()
    perf_provider = PerfHostProvider(
        instance="perf-host-fake",
        config={},
        secrets=_NoSecrets(),
        system_probe=fake_probe,
    )

    async def perf_lose_sandbox(sandbox: Sandbox) -> None:
        if hasattr(sandbox, "mark_lost"):
            sandbox.mark_lost()

    async def perf_fail_create(count: int = 1) -> None:
        perf_provider.fail_next_creates(count, SandboxCreateError)

    async def perf_expire_lifetime() -> None:
        if perf_provider._active_sandbox:
            perf_provider._active_sandbox.created_at = (
                time.time() - perf_provider._active_sandbox.spec.max_lifetime_s - 10
            )

    target_perf = ConformanceTarget(
        name="perf-host-fake",
        kind="sandbox",
        provider=perf_provider,
        fake=True,
        spec_factory=_spec_factory_perf_host,
        injectors={
            "lose_sandbox": perf_lose_sandbox,
            "fail_create": perf_fail_create,
            "expire_lifetime": perf_expire_lifetime,
        },
        notes="Bare-metal performance host running against a fake system probe; not verified on a real host",
    )

    return [target_dev, target_perf]
