"""Hyper-V sandbox provider for Windows and Linux guests (TECH-DESIGN section 7.4.6).

Every trial gets its own Generation 2 Hyper-V VM: a differencing VHD chained to
the sha256-pinned golden image, sized with ``Set-VMProcessor``/``Set-VMMemory``,
connected to a vSwitch chosen by the network policy (Private for ``off``,
Internal plus ``Add-VMNetworkAdapterAcl`` rules pinned to the egress proxy for
allowlists, External for ``full``), booted and probed on its OpenSSH port.
Commands then travel over SSH (asyncssh) and files over SFTP; destruction is
``Stop-VM -TurnOff`` + ``Remove-VM -Force`` + deleting the differencing disk.

All host operations go through a ``HostShell`` that runs PowerShell: the real
implementation spawns ``powershell.exe``, tests inject a fake that records the
scripts and simulates VM state. The provider registry under ``state_dir``
carries the sandbox labels and the lifetime guard so ``list`` and ``reap`` can
find orphans by label.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import platform
import re
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
    TransferError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
    unenforceable_paths,
)
from evalspleef.spi.secrets import SecretResolver

from .host_shell import HostShell, PowerShellHostShell, ps_quote
from .isolation import isolation_profile
from .sandbox import HyperVSandbox

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9_.@-]+")
_ZERO_DIGEST = "sha256:" + "0" * 64
_USABLE_NET_MODES = frozenset(
    {NetMode.OFF, NetMode.FULL, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
)
Entry = dict[str, Any]
EntryList = list[Entry]


class HyperVProvider(SandboxProvider):
    """Sandbox provider backed by the Hyper-V role on a Windows host."""

    name = "hyperv"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
    ) -> None:
        super().__init__(instance, config, secrets)
        shell = config.get("host_shell")
        if shell is not None:
            # Injected implementation (the tests' FakeHostShell): use it as-is.
            self._shell = cast(HostShell, shell)
        else:
            self._shell = PowerShellHostShell(
                powershell_bin=str(config.get("powershell_bin", "powershell.exe")),
                default_timeout_s=float(config.get("host_command_timeout_s", 120.0)),
            )
        self._state_dir = Path(str(config.get("state_dir", "~/.evalspleef/hyperv"))).expanduser()
        self._registry_dir = self._state_dir / "sandboxes"
        self._registry_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir = self._state_dir / "checkpoints"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._vm_dir = Path(str(config.get("vm_dir", self._state_dir / "vhds"))).expanduser()
        self._vm_dir.mkdir(parents=True, exist_ok=True)
        self._private_switch = str(config.get("private_switch", "spleef-private"))
        self._internal_switch = str(config.get("internal_switch", "spleef-internal"))
        self._external_switch = str(config.get("external_switch", "spleef-external"))
        self._max_concurrency = _config_int(config, "max_concurrency", 4)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 14400)
        self._start_timeout_s = float(config.get("start_timeout_s", 300.0))
        self._ssh_port = _config_int(config, "ssh_port", 22)
        self._ssh_user = str(config.get("ssh_user", "agent"))
        self._ssh_key = config.get("ssh_key")
        self._ssh_password = config.get("ssh_password")
        self._ssh_known_hosts = config.get("ssh_known_hosts")
        self._ssh_connect_timeout_s = float(config.get("ssh_connect_timeout_s", 15.0))
        self._transfer = str(config.get("transfer", "sftp"))
        if self._transfer not in ("sftp", "ps-direct"):
            raise ValueError("transfer must be 'sftp' or 'ps-direct'")
        self._ps_direct_user = str(config.get("ps_direct_user", self._ssh_user))
        self._ps_direct_password = config.get("ps_direct_password")
        self._rdp_port = _config_int(config, "rdp_port", 3389)
        self._ttl_sweep_interval_s = _config_int(config, "ttl_sweep_interval_s", 30)
        self._live: set[str] = set()
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []
        self._sweepers: dict[asyncio.AbstractEventLoop, asyncio.Task[None]] = {}

    # -- capabilities and profile ------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.WINDOWS, OSFamily.LINUX}),
            arches=frozenset({Arch.X86_64}),
            isolation=Isolation.L3,
            net_modes=_USABLE_NET_MODES,
            allowlist_kinds=frozenset({"ipv4", "cidr"}),
            dynamic_network_policy=False,
            streaming=True,
            stdin=True,
            snapshot="memory",
            snapshot_fanout=True,
            pause_resume=False,
            gui="rdp",
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset({"cpu", "memory"}),
            max_concurrency=self._max_concurrency,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=120.0,
            reset_cost_s=120.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile(Isolation.L3)

    async def preflight(self) -> dict[str, Any]:
        """Host facts: PowerShell and Hyper-V module presence, capacity, slots."""
        try:
            version = await self._shell.run("$PSVersionTable.PSVersion.ToString()", timeout_s=30.0)
            if version.exit_code != 0:
                raise SpleefError(f"host shell not usable: {version.stderr.strip()}")
            hyperv = await self._shell.run(
                "(Get-Command Get-VM -ErrorAction Stop).Source", timeout_s=30.0
            )
        except SpleefError:
            raise
        except Exception as exc:
            raise SpleefError(f"hyperv preflight failed: {exc}") from exc
        if hyperv.exit_code != 0:
            raise SpleefError(
                f"the Hyper-V PowerShell module is not available: {hyperv.stderr.strip()}"
            )
        return {
            "provider": f"{self.name}/{self.instance}",
            "host_os": platform.system().lower(),
            "powershell_version": version.stdout.strip(),
            "hyperv_module": hyperv.stdout.strip(),
            "max_concurrency": self._max_concurrency,
            "live_sandboxes": len(self._live),
            "transfer": self._transfer,
        }

    # -- test-side controls (used by the conformance injectors) -------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    async def lose(self, vm_name: str) -> None:
        """Simulate the host reclaiming a live VM (turn off + delete + disk)."""
        await self._destroy_vm(vm_name)

    async def expire_live_sandboxes(self) -> None:
        """Destroy every live sandbox, mirroring a TTL expiry sweep."""
        for vm_name in list(self._live):
            await self._destroy_vm(vm_name)

    def is_live(self, vm_name: str) -> bool:
        return vm_name in self._live

    async def close(self) -> None:
        """Stop the TTL sweepers and let the host shell release what it holds.

        A sweeper belongs to the loop that created it: one whose loop is already
        closed took the task with it, one running in another thread is asked to
        cancel itself.
        """
        current = asyncio.get_running_loop()
        for loop, task in list(self._sweepers.items()):
            if task.done() or loop.is_closed():
                continue
            if loop is current:
                task.cancel()
            else:
                loop.call_soon_threadsafe(task.cancel)
        self._sweepers.clear()
        aclose = getattr(self._shell, "aclose", None)
        if aclose is not None:
            await aclose()

    # -- SandboxProvider -----------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        self._validate_spec(spec)
        if len(self._live) + self._pending >= self._max_concurrency:
            raise QuotaExceeded(
                f"hyperv provider {self.instance!r} is at max_concurrency {self._max_concurrency}",
                detail={"provider": self.instance, "max_concurrency": self._max_concurrency},
            )
        self._pending += 1
        try:
            vm_name, entry = await self._create_vm(spec, parent_vhdx=None)
        finally:
            self._pending -= 1
        self._live.add(vm_name)
        self._write_registry(entry)
        self._ensure_sweeper()
        return HyperVSandbox(self, vm_name, spec)

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        """Restore a checkpoint into a fresh VM (template speed-up only)."""
        self._validate_spec(spec)
        if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id) or ".." in snapshot_id:
            raise SandboxCreateError(
                f"malformed hyperv snapshot id {snapshot_id!r}",
                detail={"snapshot_id": snapshot_id},
            )
        meta_path = self._snapshot_dir / f"{snapshot_id}.json"
        if not meta_path.exists():
            raise SandboxCreateError(
                f"unknown hyperv snapshot {snapshot_id!r}",
                detail={"snapshot_id": snapshot_id},
            )
        meta = cast(Entry, json.loads(meta_path.read_text(encoding="utf-8")))
        if meta.get("os") is not None and meta["os"] != spec.os.value:
            raise CapabilityNotSupported(
                f"snapshot {snapshot_id!r} holds a {meta['os']!r} guest, "
                f"got spec for {spec.os.value!r}"
            )
        self._pending += 1
        try:
            vm_name, entry = await self._create_vm(spec, parent_vhdx=str(meta["disk"]))
        finally:
            self._pending -= 1
        self._live.add(vm_name)
        self._write_registry(entry)
        self._ensure_sweeper()
        return HyperVSandbox(self, vm_name, spec)

    async def list(self, labels: Mapping[str, str]) -> EntryList:
        """Registry entries matching every given label whose VM still exists."""
        names = await self._vm_names()
        matches: EntryList = []
        for entry in self._entries():
            if not _labels_match(entry.get("labels", {}), labels):
                continue
            vm_name = str(entry.get("id", ""))
            if not vm_name:
                continue
            if names is not None and vm_name not in names:
                continue
            matches.append(
                {
                    "id": vm_name,
                    "labels": dict(entry.get("labels", {})),
                    "state": "running",
                    "created_at": entry.get("created_at", 0),
                    "expires_at": entry.get("expires_at", 0),
                    "os": entry.get("os"),
                }
            )
        return matches

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Destroy registry entries matching ``labels`` that are old or past their TTL."""
        now = time.time()
        reaped = 0
        for entry in self._entries():
            if not _labels_match(entry.get("labels", {}), labels):
                continue
            vm_name = str(entry.get("id", ""))
            created = float(entry.get("created_at") or 0)
            expires_at = float(entry.get("expires_at") or 0)
            past_age = created > 0 and (now - created) >= older_than_s
            past_ttl = expires_at > 0 and expires_at <= now
            if not (past_age or past_ttl):
                continue
            await self._destroy_vm(vm_name)
            reaped += 1
        return reaped

    async def release(self, vm_name: str) -> None:
        """Idempotent destroy: turn off, remove the VM and delete the differencing disk."""
        self._live.discard(vm_name)
        await self._destroy_vm(vm_name)

    @property
    def transfer(self) -> str:
        return self._transfer

    @property
    def ssh_user(self) -> str:
        return self._ssh_user

    @property
    def ssh_key(self) -> Any:
        return self._ssh_key

    @property
    def ssh_password(self) -> Any:
        return self._ssh_password

    @property
    def ssh_known_hosts(self) -> Any:
        return self._ssh_known_hosts

    @property
    def ssh_connect_timeout_s(self) -> float:
        return self._ssh_connect_timeout_s

    async def describe_vm(self, vm_name: str) -> dict[str, Any]:
        """The registry facts for one VM (template digest actually observed)."""
        entry = self._entry(vm_name)
        if not entry:
            return {}
        return {
            "sandbox_id": vm_name,
            "template": entry.get("template"),
            "digest": entry.get("digest"),
            "os": entry.get("os"),
            "cpus": entry.get("cpus"),
            "memory_mb": entry.get("memory_mb"),
            "network": entry.get("network"),
            "host": "hyper-v",
        }

    async def checkpoint(self, vm_name: str, name: str) -> str:
        """Take a VM checkpoint (memory included) and return its snapshot id."""
        snapshot_name = f"spleef-{name}"
        result = await self._shell.run(
            f"Checkpoint-VM -Name {ps_quote(vm_name)} -SnapshotName {ps_quote(snapshot_name)}",
            timeout_s=300.0,
        )
        if result.exit_code != 0:
            raise SpleefError(
                f"Checkpoint-VM on {vm_name!r} failed: {result.stderr.strip()}",
                detail={"sandbox_id": vm_name},
            )
        disk = await self._shell.run(
            f"(Get-VMSnapshot -VMName {ps_quote(vm_name)} -Name {ps_quote(snapshot_name)}).HardDrives[0].Path",
            timeout_s=60.0,
        )
        if disk.exit_code != 0 or not disk.stdout.strip():
            raise SpleefError(
                f"cannot resolve the checkpoint disk of {vm_name!r}: {disk.stderr.strip()}",
                detail={"sandbox_id": vm_name},
            )
        entry = self._entry(vm_name)
        # The id doubles as a file name on the Windows host, so it stays filename-safe.
        snapshot_id = f"{vm_name}@{_NAME_SAFE_RE.sub('-', name)}"
        meta = {
            "vm": vm_name,
            "name": name,
            "snapshot_name": snapshot_name,
            "disk": disk.stdout.strip(),
            "os": entry.get("os"),
            "template": entry.get("template"),
            "digest": entry.get("digest"),
            "created_at": time.time(),
        }
        (self._snapshot_dir / f"{snapshot_id}.json").write_text(
            json.dumps(meta, sort_keys=True), encoding="utf-8"
        )
        return snapshot_id

    async def guest_address(self, vm_name: str) -> tuple[str, int]:
        """The ``(host, port)`` at which the guest's OpenSSH port answers."""
        result = await self._shell.run(
            f"(Get-VMNetworkAdapter -Name 'Network Adapter' -VMName {ps_quote(vm_name)}).IPAddresses",
            timeout_s=60.0,
        )
        if result.exit_code != 0:
            raise SandboxLost(
                detail={"sandbox_id": vm_name, "reason": f"guest IP query failed: {result.stderr}"}
            )
        for token in result.stdout.split():
            host: str
            port: int
            if token.startswith("::") or (":" in token and token.count(":") > 1):
                continue  # skip IPv6 for now; the first IPv4 wins
            if ":" in token:
                host, _, raw_port = token.rpartition(":")
                with contextlib.suppress(ValueError):
                    port = int(raw_port)
                    return host, port
            else:
                return token, self._ssh_port
        raise SandboxLost(
            detail={"sandbox_id": vm_name, "reason": "the guest has no reachable IPv4 address"}
        )

    async def rdp_endpoint(self, vm_name: str) -> str:
        host, _ = await self.guest_address(vm_name)
        return f"rdp://{host}:{self._rdp_port}"

    async def ps_direct_copy(
        self,
        vm_name: str,
        *,
        source: str,
        destination: str,
        to_guest: bool,
    ) -> None:
        """Copy one host file to/from the guest through PowerShell Direct.

        This is the no-network fallback for file transfer: it needs no SSH/SFTP
        listener at all, only the Hyper-V VMBus, so it also works while the
        guest is configured network-off.
        """
        user = self._ps_direct_user
        password_ref = self._ps_direct_password
        if password_ref is None:
            raise SpleefError(
                "ps-direct transfer needs config 'ps_direct_password' (a SecretRef)",
                detail={"sandbox_id": vm_name},
            )
        password = self.secrets.resolve(password_ref)
        direction = "-ToSession" if to_guest else "-FromSession"
        script = (
            f"$cred = New-Object System.Management.Automation.PSCredential("
            f"{ps_quote(user)}, (ConvertTo-SecureString {ps_quote(password)} -AsPlainText -Force)); "
            f"$session = New-PSSession -VMName {ps_quote(vm_name)} -Credential $cred; "
            f"Copy-Item -Path {ps_quote(source)} -Destination {ps_quote(destination)} "
            f"-Recurse {direction} $session; "
            "Remove-PSSession $session"
        )
        result = await self._shell.run(script, timeout_s=600.0)
        if result.exit_code != 0:
            raise TransferError(
                f"ps-direct copy failed: {result.stderr.strip()}",
                detail={"sandbox_id": vm_name},
            )

    # -- VM lifecycle ---------------------------------------------------------------

    async def _create_vm(
        self,
        spec: SandboxSpec,
        *,
        parent_vhdx: str | None,
    ) -> tuple[str, Entry]:
        vm_name = self._vm_name(spec.trial_id)
        vhdx = str(self._vm_dir / f"{vm_name}.vhdx")
        parent = parent_vhdx if parent_vhdx is not None else spec.template.ref
        cpus = max(1, round(spec.resources.cpus))
        try:
            await self._run_checked(
                "New-VHD",
                f"New-VHD -Path {ps_quote(vhdx)} -ParentPath {ps_quote(parent)} -Differencing",
            )
            await self._run_checked(
                "New-VM",
                f"New-VM -Name {ps_quote(vm_name)} -Generation 2 -VHDPath {ps_quote(vhdx)}",
            )
            await self._run_checked(
                "Set-VMProcessor",
                f"Set-VMProcessor -VMName {ps_quote(vm_name)} -Count {cpus}",
            )
            await self._run_checked(
                "Set-VMMemory",
                f"Set-VMMemory -VMName {ps_quote(vm_name)} "
                f"-StartupBytes {spec.resources.memory_mb}MB -DynamicMemoryEnabled $false",
            )
            await self._connect_network(spec, vm_name)
            await self._run_checked("Start-VM", f"Start-VM -Name {ps_quote(vm_name)}")
            await self._wait_for_ssh(vm_name)
        except BaseException as exc:
            await self._destroy_vm(vm_name)
            if isinstance(exc, (SpleefError, asyncio.CancelledError)):
                raise
            raise SandboxCreateError(
                f"failed to create hyperv VM {vm_name!r}: {exc}",
                detail={"template": spec.template.ref},
            ) from exc
        now = time.time()
        labels = dict(spec.labels)
        labels["spleef.provider"] = self.instance
        labels["spleef.trial"] = spec.trial_id
        labels["spleef.lease"] = spec.lease_id
        entry: Entry = {
            "id": vm_name,
            "labels": labels,
            "created_at": now,
            "expires_at": now + spec.max_lifetime_s,
            "os": spec.os.value,
            "cpus": cpus,
            "memory_mb": spec.resources.memory_mb,
            "template": spec.template.ref,
            "digest": spec.template.digest,
            "vhdx": vhdx,
            "network": spec.network.mode.value,
            "checkpoints": {},
        }
        return vm_name, entry

    async def _connect_network(self, spec: SandboxSpec, vm_name: str) -> None:
        policy = spec.network
        if policy.mode is NetMode.OFF:
            switch = self._private_switch
        elif policy.mode is NetMode.FULL:
            switch = self._external_switch
        else:
            switch = self._internal_switch
        await self._run_checked(
            "Connect-VMNetworkAdapter",
            f"Connect-VMNetworkAdapter -VMName {ps_quote(vm_name)} -SwitchName {ps_quote(switch)}",
        )
        if policy.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
            allowed = [str(_literal_ip(cidr)) for cidr in policy.allow_cidrs]
            if policy.via_proxy:
                allowed.append(_proxy_ip(policy.via_proxy))
            await self._run_checked(
                "Add-VMNetworkAdapterAcl",
                f"Add-VMNetworkAdapterAcl -VMName {ps_quote(vm_name)} "
                f"-RemoteAddress {','.join(allowed)} -Direction Both -Action Allow",
            )

    async def _wait_for_ssh(self, vm_name: str) -> None:
        """Poll the guest's OpenSSH port until it answers or the budget runs out."""
        deadline = time.monotonic() + self._start_timeout_s
        last_error = "no IP address yet"
        while time.monotonic() < deadline:
            try:
                host, port = await self.guest_address(vm_name)
            except SandboxLost:
                await asyncio.sleep(0.25)
                continue
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
            except OSError as exc:
                last_error = str(exc)
                await asyncio.sleep(0.25)
                continue
            except TimeoutError:
                last_error = f"tcp connect to {host}:{port} timed out"
                await asyncio.sleep(0.25)
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        raise SandboxCreateError(
            f"OpenSSH on {vm_name!r} was not reachable within "
            f"{self._start_timeout_s:g}s ({last_error})"
        )

    async def _destroy_vm(self, vm_name: str) -> None:
        self._live.discard(vm_name)
        vhdx = str(self._vm_dir / f"{vm_name}.vhdx")
        script = (
            f"$vm = Get-VM -Name {ps_quote(vm_name)} -ErrorAction SilentlyContinue; "
            "if ($vm) { "
            f"Stop-VM -Name {ps_quote(vm_name)} -TurnOff -Force -ErrorAction SilentlyContinue; "
            f"Remove-VM -Name {ps_quote(vm_name)} -Force -ErrorAction SilentlyContinue "
            "}; "
            f"Remove-Item -LiteralPath {ps_quote(vhdx)} -Recurse -Force "
            "-ErrorAction SilentlyContinue"
        )
        with contextlib.suppress(Exception):
            await self._shell.run(script, timeout_s=180.0)
        with contextlib.suppress(FileNotFoundError):
            self._registry_path(vm_name).unlink()

    async def _run_checked(self, operation: str, script: str) -> None:
        result = await self._shell.run(script, timeout_s=300.0)
        if result.exit_code != 0:
            raise SandboxCreateError(
                f"{operation} failed: {result.stderr.strip() or result.stdout.strip()}",
                detail={"operation": operation, "script": script},
            )

    # -- registry ---------------------------------------------------------------------

    def _vm_name(self, trial_id: str) -> str:
        safe = _NAME_SAFE_RE.sub("-", trial_id)[:48] or "trial"
        return f"spleef-{safe}-{uuid.uuid4().hex[:8]}"

    def _registry_path(self, vm_name: str) -> Path:
        return self._registry_dir / f"{vm_name}.json"

    def _entry(self, vm_name: str) -> dict[str, Any]:
        path = self._registry_path(vm_name)
        if not path.exists():
            return {}
        return cast(Entry, json.loads(path.read_text(encoding="utf-8")))

    def _write_registry(self, entry: Mapping[str, Any]) -> None:
        path = self._registry_path(str(entry["id"]))
        path.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")

    def _entries(self) -> EntryList:
        entries: EntryList = []
        for path in sorted(self._registry_dir.glob("*.json")):
            try:
                entries.append(cast(Entry, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        return entries

    async def _vm_names(self) -> set[str] | None:
        result = await self._shell.run(
            "Get-VM | Select-Object -ExpandProperty Name", timeout_s=60.0
        )
        if result.exit_code != 0:
            return None
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    # -- TTL sweep --------------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        loop = asyncio.get_running_loop()
        task = self._sweepers.get(loop)
        if task is not None and not task.done():
            return
        self._sweepers[loop] = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ttl_sweep_interval_s)
            try:
                await self._sweep_once()
            except Exception:
                # The sweep is a safety net; a transient host error must not kill it.
                continue

    async def _sweep_once(self) -> None:
        now = time.time()
        for entry in self._entries():
            expires_at = float(entry.get("expires_at") or 0)
            if expires_at and expires_at <= now:
                await self._destroy_vm(str(entry.get("id", "")))

    # -- spec validation -----------------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> None:
        caps = self.capabilities()
        missing = unenforceable_paths(spec, caps)
        if missing:
            raise PolicyNotEnforceable(
                f"{self.name} cannot mount declared writable paths: {missing}"
            )
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(
                f"hyperv runs windows and linux guests only, got {spec.os.value!r}"
            )
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(f"hyperv on x86_64 cannot run arch {spec.arch.value!r}")
        if spec.template.kind != "vhdx":
            raise CapabilityNotSupported(
                f"hyperv needs a vhdx template, got {spec.template.kind!r}"
            )
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("hyperv GPU-P passthrough is not supported")
        if spec.max_lifetime_s > self._max_lifetime_s:
            raise CapabilityNotSupported(
                f"hyperv max_lifetime_s is {self._max_lifetime_s}, requested {spec.max_lifetime_s}"
            )
        if spec.env:
            raise CapabilityNotSupported(
                "hyperv applies environment variables per exec, not at create"
            )
        self._validate_network(spec.network)

    def _validate_network(self, policy: Any) -> None:
        if policy.mode is NetMode.OFF or policy.mode is NetMode.FULL:
            if policy.allow_hosts or policy.allow_cidrs or policy.via_proxy:
                raise PolicyNotEnforceable(
                    f"network mode {policy.mode.value!r} cannot carry allowlist rules; "
                    "use docs_whitelist or package_mirror"
                )
            return
        if policy.via_proxy and policy.allow_cidrs:
            raise PolicyNotEnforceable(
                "with via_proxy only the proxy address is allowed; drop allow_cidrs"
            )
        if policy.allow_hosts and not policy.via_proxy:
            raise PolicyNotEnforceable(
                "port ACLs accept IP addresses and CIDRs only; hostname allowlists need via_proxy"
            )
        if not policy.via_proxy and not policy.allow_cidrs:
            raise PolicyNotEnforceable(
                f"network mode {policy.mode.value!r} has no allowlist rules and no "
                "via_proxy; refusing to degrade to open networking"
            )
        if policy.via_proxy:
            _proxy_ip(policy.via_proxy)
        for cidr in policy.allow_cidrs:
            _literal_ip(cidr)


def _labels_match(labels: Mapping[str, str], wanted: Mapping[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in wanted.items())


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _proxy_ip(address: str) -> str:
    """The IPv4 address part of a proxy endpoint (``http://10.0.0.5:3128`` -> ``10.0.0.5``)."""
    raw = address
    if "://" in raw:
        parsed = urlsplit(raw)
        raw = parsed.hostname or ""
    elif raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError as exc:
        raise PolicyNotEnforceable(
            f"egress proxy address {address!r} is not a literal IP; hyperv port ACLs "
            "accept IP addresses only"
        ) from exc


def _literal_ip(cidr: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise PolicyNotEnforceable(f"invalid CIDR {cidr!r}: {exc}") from exc
