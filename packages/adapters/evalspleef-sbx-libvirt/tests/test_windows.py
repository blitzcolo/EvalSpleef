"""Windows guest support: command semantics and domain XML (no real Windows run)."""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from evalspleef_sbx_libvirt._fake import FakeLibvirtHost
from evalspleef_sbx_libvirt.provider import LibvirtProvider, domain_xml
from evalspleef_sbx_libvirt.sandbox import windows_command, windows_script

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.sandbox import Resources


def test_windows_script_uses_powershell_semantics() -> None:
    script = windows_script(
        ["powershell", "-NoProfile"], cwd="C:\\workspace", env={"A": "b"}, timeout_s=None
    )
    assert "Set-Location -LiteralPath 'C:\\workspace'" in script
    assert "$env:A = 'b'" in script
    assert "& 'powershell' '-NoProfile'" in script
    assert script.endswith("exit $LASTEXITCODE")


def test_windows_command_is_transported_as_powershell_command() -> None:
    command = windows_command(["cmd", "/c", "exit 7"], cwd=None, env=None, timeout_s=5.0)
    assert command.startswith("powershell -NoProfile -NonInteractive -Command ")
    assert "[ScriptBlock]::Create" in command
    encoded = command.split("FromBase64String('", 1)[1].split("')", 1)[0]
    script = base64.b64decode(encoded).decode("utf-8")
    assert "Start-Process" in script
    assert "taskkill.exe /PID $child.Id /T /F" in script
    assert "exit 124" in script


def test_windows_domain_xml(fake_host: FakeLibvirtHost) -> None:
    base = fake_host.spec_factory("win-1", network=NetMode.OFF, max_lifetime_s=600)
    spec = replace(
        base,
        os=OSFamily.WINDOWS,
        arch=Arch.X86_64,
        resources=Resources(cpus=2, memory_mb=2048, disk_mb=2048),
        gui=True,
    )
    xml = domain_xml(
        name="spleef-win-1",
        spec=spec,
        overlay=Path("/tmp/overlay.qcow2"),
        labels={"spleef.provider": "libvirt-test"},
        network="default",
        nwfilter=None,
        created_at=1,
        expires_at=2,
        vnc_port=5900,
        rdp_port=3389,
    )
    root = ET.fromstring(xml)
    disk = root.find("./devices/disk/target")
    assert disk is not None and disk.get("dev") == "sda" and disk.get("bus") == "sata"
    graphics = root.find("./devices/graphics")
    assert graphics is not None and graphics.get("type") == "rdp"
    os_type = root.find("./os/type")
    assert os_type is not None and os_type.get("arch") == "x86_64"


async def test_windows_only_provider_declares_rdp(fake_host: FakeLibvirtHost) -> None:
    instance: LibvirtProvider = fake_host.provider(config={"guest_os": ["windows"], "gui": "rdp"})
    try:
        caps = instance.capabilities()
        assert caps.gui == "rdp"
        assert caps.os_families == frozenset({OSFamily.WINDOWS})
        spec = fake_host.spec_factory("win-2", network=NetMode.OFF, max_lifetime_s=600)
        with pytest.raises(CapabilityNotSupported):
            await instance.create(spec)
    finally:
        await instance.close()
