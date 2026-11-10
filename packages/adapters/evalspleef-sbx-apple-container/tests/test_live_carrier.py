"""Checks against the real ``container`` CLI, behind the same gate as ``apple-container-live``.

They run only when the ``container`` executable is on PATH and
``SPLEEF_APPLE_CONTAINER_IMAGE`` names a local image; elsewhere they are skipped.
They prove a real VM answered (guest identity and the limits the spec asked for)
and that every executable I1-I9 preflight probe holds on the carrier.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from evalspleef_sbx_apple_container import conformance, isolation
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import NetMode
from evalspleef.spi.sandbox import WritablePath

pytestmark = pytest.mark.skipif(
    not (os.environ.get(conformance.LIVE_IMAGE_ENV) and shutil.which("container")),
    reason=f"needs the container CLI on PATH and {conformance.LIVE_IMAGE_ENV}",
)


@pytest.fixture
async def live_target() -> AsyncIterator[ConformanceTarget]:
    targets = conformance.build_targets()
    try:
        yield next(target for target in targets if target.name == conformance.LIVE_TARGET_NAME)
    finally:
        for target in targets:
            assert target.cleanup is not None
            await target.cleanup()


async def test_a_real_guest_answers_with_the_requested_limits(
    live_target: ConformanceTarget,
) -> None:
    async with live_target.open_sandbox("live-identity") as sandbox:
        resources = sandbox.spec.resources
        result = await sandbox.exec(
            "uname -s; uname -r; id -un; cut -d' ' -f1 /sys/fs/cgroup/cpu.max; "
            "cat /sys/fs/cgroup/memory.max"
        )
        assert result.exit_code == 0, result.stderr
        system, release, user, quota, memory = result.stdout.decode().split()
        assert system == "Linux"
        assert release != platform.release()  # the guest kernel, not the host's
        assert user == sandbox.spec.run_as_user
        assert int(quota) == int(resources.cpus * 100_000)
        assert int(memory) == resources.memory_mb << 20


async def test_reported_environment_matches_the_real_guest(live_target: ConformanceTarget) -> None:
    provider = live_target.provider
    assert isinstance(provider, AppleContainerProvider)
    report = await provider.environment()
    version = (await provider.cli.run(["--version"])).stdout_text.split()
    async with live_target.open_sandbox("live-environment") as sandbox:
        guest = await sandbox.exec(["uname", "-r"])
    assert guest.exit_code == 0, guest.stderr
    assert report.guest.kernel == guest.stdout.decode().strip()
    assert report.guest.os == "linux"
    assert report.runtime.name == version[0] == "container"
    assert report.runtime.version == version[3]
    assert report.host.kernel == platform.release()
    assert report.extra["default_kernel_image"].startswith("vmlinux")


def _nested_tree(root: Path) -> Path:
    tree = root / "workspace"
    (tree / "src" / "pkg").mkdir(parents=True)
    (tree / "README.md").write_bytes(b"top\n")
    (tree / "src" / "pkg" / "mod.py").write_bytes(b"VALUE = 1\n")
    return tree


async def test_a_directory_uploads_as_the_workspace_mount_point(
    live_target: ConformanceTarget,
) -> None:
    async with live_target.open_sandbox("live-workspace-upload") as sandbox:
        with tempfile.TemporaryDirectory() as tmp:
            tree = await asyncio.to_thread(_nested_tree, Path(tmp))
            await sandbox.upload(tree, "/workspace")
        read_back = await sandbox.exec(["cat", "/workspace/README.md", "/workspace/src/pkg/mod.py"])
        owner = await sandbox.exec(["stat", "-c", "%U", "/workspace/src/pkg/mod.py"])
    assert read_back.exit_code == 0, read_back.stderr
    assert read_back.stdout == b"top\nVALUE = 1\n"
    assert owner.stdout.decode().strip() == sandbox.spec.run_as_user


async def test_a_declared_verify_root_is_writable(live_target: ConformanceTarget) -> None:
    provider = live_target.provider
    assert isinstance(provider, AppleContainerProvider)
    assert live_target.spec_factory is not None
    spec = live_target.spec_factory("live-verifier", network=NetMode.OFF, max_lifetime_s=600)
    declared = (WritablePath("/verify", 32),)
    verifier = await provider.create(replace(spec, role="verifier", writable_paths=declared))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "test_hidden.sh")
            await asyncio.to_thread(source.write_bytes, b"echo hidden\n")
            await verifier.upload(source, "/verify/hidden-tests/test_hidden.sh")
        written = await verifier.exec(
            "printf ok > /verify/result.txt && cat /verify/hidden-tests/test_hidden.sh "
            "/verify/result.txt"
        )
    finally:
        await verifier.destroy()
    assert written.exit_code == 0, written.stderr
    assert written.stdout == b"echo hidden\nok"


@pytest.mark.parametrize("network", [NetMode.OFF, NetMode.FULL])
async def test_preflight_probes_hold_on_the_carrier(
    live_target: ConformanceTarget, network: NetMode
) -> None:
    profile = isolation.isolation_profile()
    async with live_target.open_sandbox(f"live-probes-{network.value}", network=network) as sandbox:
        failures = []
        for key in (f"I{index}" for index in range(1, 10)):
            command = profile.invariants[key].command
            assert command is not None, key
            result = await sandbox.exec(command, timeout_s=30)
            if result.exit_code != 0:
                failures.append(f"{key}: exit {result.exit_code} {result.stderr!r}")
        root = await sandbox.exec(["grep", "^CapEff:", "/proc/self/status"], user="root")
    assert failures == []
    assert root.stdout.split()[1].strip(b"0") == b"", root.stdout
