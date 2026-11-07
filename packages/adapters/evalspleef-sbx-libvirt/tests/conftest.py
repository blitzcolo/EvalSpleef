"""Fixtures for the libvirt adapter tests.

Every test runs against fake ``virsh``/``qemu-img`` executables on a temporary
PATH and a real local asyncssh server acting as the guest. No hypervisor and no
real guest is involved, so nothing here is verified on a real host.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from evalspleef_sbx_libvirt._fake import FakeLibvirtHost
from evalspleef_sbx_libvirt.provider import LibvirtProvider
from evalspleef_sbx_libvirt.sandbox import LibvirtSandbox

from evalspleef.spi.caps import NetMode


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeLibvirtHost]:
    host = FakeLibvirtHost().start()
    monkeypatch.setenv("PATH", f"{host.bin_dir}{os.pathsep}{os.environ['PATH']}")
    try:
        yield host
    finally:
        host.stop()


@pytest.fixture
async def provider(fake_host: FakeLibvirtHost) -> AsyncIterator[LibvirtProvider]:
    instance = fake_host.provider()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def sandbox(
    provider: LibvirtProvider, fake_host: FakeLibvirtHost
) -> AsyncIterator[LibvirtSandbox]:
    instance = await provider.create(
        fake_host.spec_factory("unit-1", network=NetMode.OFF, max_lifetime_s=600)
    )
    assert isinstance(instance, LibvirtSandbox)
    try:
        yield instance
    finally:
        await instance.destroy()
