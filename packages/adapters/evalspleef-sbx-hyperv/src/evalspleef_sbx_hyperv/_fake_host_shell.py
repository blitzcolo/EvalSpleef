"""A fake Hyper-V host for tests and the conformance target.

``FakeHostShell`` implements the ``HostShell`` protocol without PowerShell: it
records every script the provider sends and simulates the VM state those
scripts would create. A "VHDX" is a marker file plus a sibling directory of the
same name (without ``.vhdx``) that holds the guest filesystem; ``New-VHD
-Differencing`` copies the parent directory, ``Start-VM`` starts a
``FakeGuestServer`` rooted at that directory and ``Get-VMNetworkAdapter``
reports its ``127.0.0.1:<port>`` address, so the provider's SSH/SFTP code runs
against a real (local) asyncssh server.

It parses only the exact script shapes ``HyperVProvider`` emits. It is not
Hyper-V and proves nothing about a real host.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ._fake_guest import FakeGuestServer, map_guest_path
from .host_shell import HostResult


@dataclass
class FakeVM:
    """The simulated state of one VM."""

    name: str
    vhd: str
    generation: int = 2
    cpus: int = 1
    memory_mb: int = 0
    switch: str | None = None
    acl_addresses: list[str] = field(default_factory=list)
    running: bool = False


_Apply = Callable[[FakeVM, str], object]


def guest_dir(vhd_path: str) -> Path:
    """The directory that holds the guest filesystem of a (fake) VHDX path."""
    return Path(vhd_path).with_suffix("")


class FakeHostShell:
    """Records PowerShell scripts and simulates Hyper-V VM state on local disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.commands: list[str] = []
        self.vms: dict[str, FakeVM] = {}
        self.fail_next: dict[str, int] = {}
        """Substring of a script -> how many more matching scripts fail."""
        self.no_ip = False
        """When true running VMs never report an IP address (OpenSSH never comes up)."""
        self._servers: dict[str, FakeGuestServer] = {}

    # -- test controls -------------------------------------------------------------

    def fail(self, fragment: str, times: int = 1) -> None:
        """Make the next ``times`` scripts containing ``fragment`` exit non-zero."""
        self.fail_next[fragment] = times

    def guest(self, vm_name: str) -> FakeGuestServer:
        """The running fake guest of ``vm_name``."""
        return self._servers[vm_name]

    def scripts(self, fragment: str) -> list[str]:
        """Every recorded script containing ``fragment``."""
        return [script for script in self.commands if fragment in script]

    async def aclose(self) -> None:
        """Stop every fake guest server that is still running."""
        for name in list(self._servers):
            await self._stop_server(name)

    # -- HostShell ---------------------------------------------------------------------

    async def run(self, script: str, *, timeout_s: float | None = None) -> HostResult:
        self.commands.append(script)
        for fragment, remaining in list(self.fail_next.items()):
            if remaining > 0 and fragment in script:
                self.fail_next[fragment] = remaining - 1
                return HostResult(1, "", f"simulated failure of {fragment}")
        return await self._dispatch(script.strip())

    async def _dispatch(self, script: str) -> HostResult:
        if script.startswith("$PSVersionTable"):
            return _ok("5.1.26100.1 (fake)\n")
        if script.startswith("(Get-Command Get-VM"):
            return _ok("Hyper-V\n")
        if script.startswith("New-VHD"):
            return self._new_vhd(script)
        if script.startswith("New-VM"):
            return self._new_vm(script)
        if script.startswith("Set-VMProcessor"):
            return self._configure(
                script, "Count", lambda vm, value: setattr(vm, "cpus", int(value))
            )
        if script.startswith("Set-VMMemory"):
            return self._set_memory(script)
        if script.startswith("Connect-VMNetworkAdapter"):
            return self._configure(
                script, "SwitchName", lambda vm, value: setattr(vm, "switch", value)
            )
        if script.startswith("Add-VMNetworkAdapterAcl"):
            return self._configure(
                script,
                "RemoteAddress",
                lambda vm, value: vm.acl_addresses.extend(value.split(",")),
            )
        if script.startswith("Start-VM"):
            return await self._start_vm(script)
        if script.startswith("Checkpoint-VM"):
            return self._checkpoint(script)
        if script.startswith("(Get-VMSnapshot"):
            return self._snapshot_disk(script)
        if script.startswith("(Get-VMNetworkAdapter"):
            return self._ip_address(script)
        if script.startswith("Get-VM | Select-Object"):
            return _ok("".join(f"{name}\n" for name in sorted(self.vms)))
        if script.startswith("$vm = Get-VM"):
            return await self._destroy(script)
        if script.startswith("$cred = New-Object"):
            return self._ps_direct_copy(script)
        return HostResult(1, "", f"fake host shell: unrecognised script: {script[:80]}")

    # -- verbs -------------------------------------------------------------------------

    def _new_vhd(self, script: str) -> HostResult:
        path = _arg(script, "Path")
        parent = _arg(script, "ParentPath")
        if path is None or parent is None or "-Differencing" not in script:
            return _err("New-VHD needs -Path, -ParentPath and -Differencing")
        source = guest_dir(parent)
        if not source.exists():
            if source.parent != self.root / "golden":
                return _err(f"Cannot find the parent virtual hard disk {parent}")
            (source / "workspace").mkdir(parents=True)  # the golden image appears on first use
        target = guest_dir(path)
        if target.exists() or Path(path).exists():
            return _err(f"The file {path} already exists")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        Path(path).write_bytes(b"")
        return _ok("")

    def _new_vm(self, script: str) -> HostResult:
        name = _arg(script, "Name")
        vhd = _arg(script, "VHDPath")
        if name is None or vhd is None or "-Generation 2" not in script:
            return _err("New-VM needs -Name, -Generation 2 and -VHDPath")
        if name in self.vms:
            return _err(f"A virtual machine named {name} already exists")
        if not Path(vhd).exists():
            return _err(f"Cannot find the virtual hard disk {vhd}")
        (guest_dir(vhd) / "workspace").mkdir(parents=True, exist_ok=True)
        self.vms[name] = FakeVM(name=name, vhd=vhd)
        return _ok("")

    def _configure(self, script: str, flag: str, apply: _Apply) -> HostResult:
        name = _arg(script, "VMName")
        value = _arg(script, flag)
        if name is None or value is None:
            return _err(f"missing -VMName or -{flag}")
        vm = self.vms.get(name)
        if vm is None:
            return _err(f"Hyper-V was unable to find a virtual machine with name {name}")
        apply(vm, value)
        return _ok("")

    def _set_memory(self, script: str) -> HostResult:
        match = re.search(r"-StartupBytes\s+(\d+)MB", script)
        if match is None:
            return _err("Set-VMMemory needs -StartupBytes <n>MB")
        name = _arg(script, "VMName")
        vm = self.vms.get(name or "")
        if vm is None:
            return _err(f"Hyper-V was unable to find a virtual machine with name {name}")
        vm.memory_mb = int(match.group(1))
        return _ok("")

    async def _start_vm(self, script: str) -> HostResult:
        name = _arg(script, "Name")
        vm = self.vms.get(name or "")
        if vm is None:
            return _err(f"Hyper-V was unable to find a virtual machine with name {name}")
        if vm.running:
            return _ok("")
        server = FakeGuestServer(guest_dir(vm.vhd))
        await server.start()
        self._servers[vm.name] = server
        vm.running = True
        return _ok("")

    def _checkpoint(self, script: str) -> HostResult:
        name = _arg(script, "Name")
        snapshot = _arg(script, "SnapshotName")
        vm = self.vms.get(name or "")
        if vm is None or snapshot is None:
            return _err("Checkpoint-VM needs an existing -Name and a -SnapshotName")
        destination = self.root / "checkpoints" / vm.name / snapshot
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(guest_dir(vm.vhd), destination)
        return _ok("")

    def _snapshot_disk(self, script: str) -> HostResult:
        name = _arg(script, "VMName")
        snapshot = _arg(script, "Name")
        if name is None or snapshot is None:
            return _err("Get-VMSnapshot needs -VMName and -Name")
        directory = self.root / "checkpoints" / name / snapshot
        if not directory.exists():
            return _err(f"No checkpoint {snapshot} for {name}")
        return _ok(f"{directory.with_name(directory.name + '.vhdx')}\n")

    def _ip_address(self, script: str) -> HostResult:
        name = _arg(script, "VMName")
        vm = self.vms.get(name or "")
        if vm is None:
            return _err(f"Hyper-V was unable to find a virtual machine with name {name}")
        if not vm.running or self.no_ip:
            return _ok("")
        return _ok(f"127.0.0.1:{self._servers[vm.name].port}\n")

    async def _destroy(self, script: str) -> HostResult:
        name = _arg(script, "Name")
        path_match = re.search(r"Remove-Item -LiteralPath '((?:[^']|'')*)'", script)
        if name is None or path_match is None:
            return _err("unrecognised destroy script")
        if name in self.vms:
            await self._stop_server(name)
            del self.vms[name]
        vhd = path_match.group(1).replace("''", "'")
        await asyncio.to_thread(_remove_disk, vhd)
        return _ok("")

    def _ps_direct_copy(self, script: str) -> HostResult:
        """PowerShell Direct ``Copy-Item -ToSession/-FromSession`` against the fake guest disk."""
        name = _arg(script, "VMName")
        vm = self.vms.get(name or "")
        if vm is None or not vm.running:
            return _err(f"PowerShell Direct needs a running virtual machine, got {name}")
        source_arg = _arg(script, "Path")
        destination_arg = _arg(script, "Destination")
        if source_arg is None or destination_arg is None:
            return _err("Copy-Item needs -Path and -Destination")
        root = guest_dir(vm.vhd)
        if "-ToSession" in script:
            source, target = Path(source_arg), map_guest_path(root, destination_arg)
        else:
            source, target = map_guest_path(root, source_arg), Path(destination_arg)
        if not source.exists():
            return _err(f"Cannot find path {source_arg}")
        if source.is_dir():
            if target.exists():
                target = target / source.name  # Copy-Item -Recurse into an existing directory
            shutil.copytree(source, target, ignore_dangling_symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return _ok("")

    async def _stop_server(self, vm_name: str) -> None:
        server = self._servers.pop(vm_name, None)
        if server is not None:
            with contextlib.suppress(Exception):
                await server.stop()


def _remove_disk(vhd: str) -> None:
    shutil.rmtree(guest_dir(vhd), ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        Path(vhd).unlink()


def _ok(stdout: str) -> HostResult:
    return HostResult(0, stdout, "")


def _err(message: str) -> HostResult:
    return HostResult(1, "", message)


def _arg(script: str, flag: str) -> str | None:
    """The value of ``-flag 'value'`` (quoted) or ``-flag value`` (bare) in ``script``."""
    quoted = re.search(rf"(?<![\w])-{re.escape(flag)}\s+'((?:[^']|'')*)'", script)
    if quoted:
        return quoted.group(1).replace("''", "'")
    bare = re.search(rf"(?<![\w])-{re.escape(flag)}\s+([^\s'-][^\s]*)", script)
    if bare:
        return bare.group(1)
    return None
