"""AppleContainerProvider.environment() against the fake CLI, which mirrors the real output.

Expected values come from the fake's constants and the Python ``platform`` module,
not from the adapter, so a wrong or unreported field fails here.
"""

from __future__ import annotations

import platform

from evalspleef_sbx_apple_container import fake_container
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

from evalspleef.spi.environment import normalise_arch, os_family


async def test_reports_host_runtime_and_guest_kernel(
    provider: AppleContainerProvider, fake_env
) -> None:
    report = await provider.environment()

    assert report.runtime.name == "container"
    assert report.runtime.version == fake_container.VERSION
    # The guest kernel comes from the default kernel image's own banner.
    assert report.guest.kernel == fake_container.KERNEL_RELEASE
    assert report.guest.os == "linux"
    assert report.guest.arch == "arm64"
    # Sandbox VMs are sized per spec and the distribution is the image's: unreported.
    assert report.guest.cpu_count is None and report.guest.memory_bytes is None
    assert report.guest.os_version is None
    assert report.host.os == os_family(platform.system())
    assert report.host.kernel == platform.release()
    assert report.host.arch == normalise_arch(platform.machine())
    app_root = fake_env.state / "app"
    assert report.extra["app_root"] == f"{app_root}/"
    assert report.extra["default_kernel_image"] == fake_container.KERNEL_IMAGE
    assert report.extra["apiserver_version"] == fake_container.VERSION
    assert report.extra["vminit_image"] == fake_container.VMINIT_IMAGE
    assert len(report.extra["default_kernel_sha256"]) == 64


async def test_an_unchanged_carrier_reports_an_identical_environment(
    provider: AppleContainerProvider, spec_factory
) -> None:
    before = await provider.environment()
    sandbox = await provider.create(spec_factory("env-stable"))
    try:
        # A running sandbox changes volatile status counters, never the report.
        assert await provider.environment() == before
    finally:
        await sandbox.destroy()
