"""Tart sandbox provider for Apple Silicon hosts (TECH-DESIGN section 7.4.3).

Every trial gets its own VM, cloned from the golden image by digest, sized with
``tart set``, booted headless with softnet options that encode the network
policy, and destroyed with ``tart stop`` + ``tart delete``. Commands run through
``tart exec`` (guest agent) with SSH as the fallback transport. The provider
registry under ``state_dir`` carries the sandbox labels and the lifetime guard,
so ``list`` and ``reap`` can find orphans by label.

Only macOS and Linux guests on arm64 are supported; snapshot is not offered for
trials, GPUs are not available and network allowlists are CIDR-only (hostname
allowlists require an egress proxy).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import platform
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
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

from .isolation import isolation_profile
from .sandbox import TartSandbox

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_ZERO_DIGEST = "sha256:" + "0" * 64
_USABLE_NET_MODES = frozenset(
    {NetMode.OFF, NetMode.FULL, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
)
Entry = dict[str, Any]
EntryList = list[Entry]
StrList = list[str]
_SSH_TARGET_CONNECTOR = Callable[[Mapping[str, Any]], Awaitable[Any]]


class TartProvider(SandboxProvider):
    """Sandbox provider backed by the local ``tart`` CLI on an Apple Silicon host."""

    name = "tart"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._tart_bin = str(config.get("tart_bin", "tart"))
        self._host_macos_slots = _config_int(config, "host_macos_slots", 2)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 14400)
        self._start_timeout_s = float(config.get("start_timeout_s", 180.0))
        self._guest_exec_timeout_s = float(config.get("guest_exec_timeout_s", 600.0))
        self._ttl_sweep_interval_s = _config_int(config, "ttl_sweep_interval_s", 30)
        self._vnc_host = str(config.get("vnc_host", "127.0.0.1"))
        self._vnc_port = _config_int(config, "vnc_port", 5900)
        self._vnc_url_timeout_s = float(config.get("vnc_url_timeout_s", 5.0))
        self._ssh_port = _config_int(config, "ssh_port", 22)
        self._ssh_user = str(config.get("ssh_user", "admin"))
        self._ssh_key = config.get("ssh_key")
        self._ssh_known_hosts = config.get("ssh_known_hosts")
        transport = str(config.get("exec_transport", "auto"))
        if transport not in ("auto", "guest-agent", "ssh"):
            raise ValueError(f"exec_transport must be auto, guest-agent or ssh, got {transport!r}")
        self._exec_transport = transport
        self._state_dir = Path(str(config.get("state_dir", "~/.evalspleef/tart"))).expanduser()
        self._vms_dir = self._state_dir / "sandboxes"
        self._vms_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_connector: _SSH_TARGET_CONNECTOR = _connect_ssh
        self._live: set[str] = set()
        self._run_processes: dict[str, asyncio.subprocess.Process] = {}
        self._run_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._vnc_urls: dict[str, str] = {}
        self._vnc_events: dict[str, asyncio.Event] = {}
        self._sweepers: dict[asyncio.AbstractEventLoop, asyncio.Task[None]] = {}
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    # -- configuration surface -------------------------------------------------

    @property
    def exec_transport(self) -> str:
        return self._exec_transport

    @property
    def guest_exec_timeout_s(self) -> float:
        return self._guest_exec_timeout_s

    def tart_argv(self, *args: str) -> list[str]:
        return [self._tart_bin, *args]

    # -- capabilities and profile ------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.MACOS, OSFamily.LINUX}),
            arches=frozenset({Arch.ARM64}),
            isolation=Isolation.L3,
            net_modes=_USABLE_NET_MODES,
            allowlist_kinds=frozenset({"cidr"}),
            dynamic_network_policy=False,
            streaming=True,
            stdin=True,
            snapshot="none",
            snapshot_fanout=False,
            pause_resume=False,
            gui="vnc",
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset({"cpu", "memory"}),
            max_concurrency=self._host_macos_slots,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=60.0,
            reset_cost_s=60.0,
            # The macOS system volume is read-only, so /verify cannot be created.
            verify_root="/workspace/verify",
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile(Isolation.L3)

    async def preflight(self) -> dict[str, Any]:
        """Host facts: architecture, tart binary and the guest agent readiness tool."""
        arch = platform.machine().lower()
        version = await self._run_tart("--version", limit_s=30.0)
        if version.returncode != 0:
            raise SpleefError(
                f"tart preflight failed: {version.stderr.decode(errors='replace').strip()}"
            )
        return {
            "provider": f"{self.name}/{self.instance}",
            "host_arch": arch,
            "host_platform": platform.system().lower(),
            "apple_silicon": arch in ("arm64", "aarch64"),
            "tart_bin": self._tart_bin,
            "tart_version": version.stdout.decode(errors="replace").strip(),
            "host_macos_slots": self._host_macos_slots,
            "live_sandboxes": len(self._live),
            "exec_transport": self._exec_transport,
        }

    # -- test-side controls (used by the conformance injectors) -------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    async def lose(self, vm_name: str) -> None:
        """Simulate the host reclaiming a live VM (stop + delete)."""
        await self._destroy_vm(vm_name)

    async def expire_live_sandboxes(self) -> None:
        """Destroy every live sandbox, mirroring a TTL expiry sweep."""
        for vm_name in list(self._live):
            await self._destroy_vm(vm_name)

    def is_live(self, vm_name: str) -> bool:
        return vm_name in self._live

    # -- SandboxProvider -----------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        self._validate_spec(spec)
        if spec.os is OSFamily.MACOS and self._macos_sandboxes() >= self._host_macos_slots:
            raise QuotaExceeded(
                f"tart provider {self.instance!r} already runs "
                f"{self._host_macos_slots} macOS guests (host_macos_slots)",
                detail={"provider": self.instance, "host_macos_slots": self._host_macos_slots},
            )
        vm_name = self._vm_name(spec.trial_id)
        try:
            await self._clone(spec, vm_name)
            await self._configure(spec, vm_name)
            await self._boot(spec, vm_name)
            await self._wait_for_guest_agent(vm_name)
        except BaseException as exc:
            await self._destroy_vm(vm_name)
            if isinstance(exc, SpleefError):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SandboxCreateError(
                f"failed to create tart VM {vm_name!r}: {exc}",
                detail={"template": spec.template.ref},
            ) from exc
        self._write_registry(spec, vm_name)
        self._live.add(vm_name)
        self._ensure_sweeper()
        return TartSandbox(self, vm_name, spec)

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
        """Idempotent destroy: stop and delete the VM, a missing VM counts as success."""
        self._live.discard(vm_name)
        if vm_name not in self._run_processes and not self._registry_path(vm_name).exists():
            return
        await self._destroy_vm(vm_name)

    async def describe_vm(self, vm_name: str) -> dict[str, Any]:
        entry = self._entry(vm_name)
        return {
            "sandbox_id": vm_name,
            "template": entry.get("template"),
            "digest": entry.get("digest"),
            "os": entry.get("os"),
            "cpus": entry.get("cpus"),
            "memory_mb": entry.get("memory_mb"),
            "host": "apple-silicon-tart",
            "transport": "ssh" if self._exec_transport == "ssh" else "guest-agent",
        }

    async def vnc_endpoint(self, vm_name: str) -> str:
        event = self._vnc_events.setdefault(vm_name, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=self._vnc_url_timeout_s)
        return self._vnc_urls.get(vm_name) or f"vnc://{self._vnc_host}:{self._vnc_port}"

    async def ssh_target(self, vm_name: str) -> dict[str, Any]:
        ip = await self._guest_ip(vm_name)
        key: str | None = None
        if self._ssh_key is not None:
            key = self.secrets.resolve(self._ssh_key)
        return {
            "host": ip,
            "port": self._ssh_port,
            "username": self._ssh_user,
            "key": key,
            "known_hosts": self._ssh_known_hosts,
        }

    async def close(self) -> None:
        """Stop the TTL sweepers and any run processes this provider started."""
        for task in self._sweepers.values():
            task.cancel()
        self._sweepers.clear()
        for vm_name in list(self._run_processes):
            await self._stop_run_process(vm_name)

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
                f"tart runs macos and linux guests only, got {spec.os.value!r}"
            )
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(
                f"tart on Apple Silicon cannot run arch {spec.arch.value!r}"
            )
        if spec.template.kind != "tart_image":
            raise CapabilityNotSupported(
                f"tart needs a tart_image template, got {spec.template.kind!r}"
            )
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("tart guests do not expose GPUs")
        if spec.max_lifetime_s > self._max_lifetime_s:
            raise CapabilityNotSupported(
                f"tart max_lifetime_s is {self._max_lifetime_s}, requested {spec.max_lifetime_s}"
            )
        if spec.env:
            raise CapabilityNotSupported(
                "tart applies environment variables per exec, not at create"
            )
        if not spec.run_as_user:
            raise CapabilityNotSupported("tart requires a non-empty run_as_user")
        self._validate_network(spec.network)

    def _validate_network(self, policy: Any) -> None:
        if policy.mode == NetMode.OFF:
            if policy.allow_hosts or policy.allow_cidrs or policy.via_proxy:
                raise PolicyNotEnforceable(
                    "network mode 'off' cannot carry allowlist rules; drop them or use a "
                    "whitelist mode"
                )
            return
        if policy.mode == NetMode.FULL:
            if policy.allow_hosts or policy.allow_cidrs or policy.via_proxy:
                raise PolicyNotEnforceable(
                    "network mode 'full' cannot carry allowlist rules; use docs_whitelist or "
                    "package_mirror"
                )
            return
        if policy.via_proxy:
            if policy.allow_cidrs:
                raise PolicyNotEnforceable(
                    "with via_proxy only the proxy /32 is allowed; drop allow_cidrs"
                )
            _proxy_ip(policy.via_proxy)
            return
        if policy.allow_hosts:
            raise PolicyNotEnforceable(
                "tart softnet allowlists are CIDR only; hostname allowlists need via_proxy"
            )
        if not policy.allow_cidrs:
            raise PolicyNotEnforceable(
                f"network mode {policy.mode.value!r} has no allowlist rules and no via_proxy; "
                "refusing to degrade to open networking"
            )
        for cidr in policy.allow_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise PolicyNotEnforceable(f"invalid CIDR {cidr!r}: {exc}") from exc

    # -- VM lifecycle ---------------------------------------------------------------

    def _vm_name(self, trial_id: str) -> str:
        safe = _NAME_SAFE_RE.sub("-", trial_id)[:48] or "trial"
        return f"spleef-{safe}-{uuid.uuid4().hex[:8]}"

    async def _clone(self, spec: SandboxSpec, vm_name: str) -> None:
        source = spec.template.ref
        if spec.template.digest and spec.template.digest != _ZERO_DIGEST:
            source = f"{source}@{spec.template.digest}"
        result = await self._run_tart("clone", source, vm_name, limit_s=self._start_timeout_s)
        if result.returncode != 0:
            raise SandboxCreateError(
                f"tart clone {source!r} failed: {_stderr(result)}",
                detail={"template": source},
            )

    async def _configure(self, spec: SandboxSpec, vm_name: str) -> None:
        cpus = max(1, round(spec.resources.cpus))
        result = await self._run_tart(
            "set",
            vm_name,
            "--cpu",
            str(cpus),
            "--memory",
            str(spec.resources.memory_mb),
            limit_s=60.0,
        )
        if result.returncode != 0:
            raise SandboxCreateError(f"tart set on {vm_name!r} failed: {_stderr(result)}")

    async def _boot(self, spec: SandboxSpec, vm_name: str) -> None:
        argv = self.tart_argv("run", "--no-graphics")
        if spec.gui:
            argv.append("--vnc-experimental")
        argv.extend(self._net_argv(spec))
        argv.append(vm_name)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._run_processes[vm_name] = process
        self._vnc_events[vm_name] = asyncio.Event()
        self._run_tasks[vm_name] = [
            asyncio.create_task(self._pump_run_output(vm_name, process.stdout)),
            asyncio.create_task(self._pump_run_output(vm_name, process.stderr)),
        ]

    def _net_argv(self, spec: SandboxSpec) -> StrList:
        policy = spec.network
        if policy.mode == NetMode.OFF:
            return ["--net-softnet", "--net-softnet-block", "0.0.0.0/0"]
        if policy.mode == NetMode.FULL:
            return ["--net-softnet"]
        if policy.via_proxy:
            return ["--net-softnet", "--net-softnet-allow", f"{_proxy_ip(policy.via_proxy)}/32"]
        argv = ["--net-softnet"]
        for cidr in policy.allow_cidrs:
            argv.extend(["--net-softnet-allow", cidr])
        return argv

    async def _wait_for_guest_agent(self, vm_name: str) -> None:
        deadline = time.monotonic() + self._start_timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            process = self._run_processes.get(vm_name)
            if process is not None and process.returncode is not None:
                raise SandboxCreateError(
                    f"tart run for {vm_name!r} exited before the guest agent was ready"
                )
            probe = await self._run_tart(
                "exec", vm_name, "--", "/bin/sh", "-lc", "true", limit_s=30.0
            )
            if probe.returncode == 0:
                return
            last_error = _stderr(probe)
            await asyncio.sleep(0.25)
        raise SandboxCreateError(
            f"guest agent in {vm_name!r} was not ready within {self._start_timeout_s:g}s "
            f"({last_error})"
        )

    async def _destroy_vm(self, vm_name: str) -> None:
        self._live.discard(vm_name)
        with contextlib.suppress(Exception):
            await self._run_tart("stop", vm_name, limit_s=60.0)
        await self._stop_run_process(vm_name)
        with contextlib.suppress(Exception):
            await self._run_tart("delete", vm_name, limit_s=120.0)
        self._vnc_urls.pop(vm_name, None)
        self._vnc_events.pop(vm_name, None)
        with contextlib.suppress(FileNotFoundError):
            self._registry_path(vm_name).unlink()

    async def _stop_run_process(self, vm_name: str) -> None:
        process = self._run_processes.pop(vm_name, None)
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=30.0)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=15.0)
        for task in self._run_tasks.pop(vm_name, []):
            task.cancel()
        self._vnc_events.get(vm_name, asyncio.Event()).set()

    async def _pump_run_output(
        self,
        vm_name: str,
        stream: asyncio.StreamReader | None,
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            match = re.search(r"vnc://[^\s\"']+", text)
            if match:
                self._vnc_urls[vm_name] = match.group(0)
                self._vnc_events.setdefault(vm_name, asyncio.Event()).set()

    async def _guest_ip(self, vm_name: str) -> str:
        result = await self._run_tart("ip", vm_name, limit_s=30.0)
        if result.returncode != 0:
            raise SandboxLost(
                detail={"sandbox_id": vm_name, "reason": f"tart ip failed: {_stderr(result)}"}
            )
        ip = result.stdout.decode(errors="replace").strip().splitlines()
        if not ip or not ip[0]:
            raise SandboxLost(detail={"sandbox_id": vm_name, "reason": "tart ip returned nothing"})
        return ip[0]

    # -- registry ---------------------------------------------------------------------

    def _registry_path(self, vm_name: str) -> Path:
        return self._vms_dir / f"{vm_name}.json"

    def _write_registry(self, spec: SandboxSpec, vm_name: str) -> None:
        labels = dict(spec.labels)
        labels["spleef.provider"] = self.instance
        labels["spleef.trial"] = spec.trial_id
        labels["spleef.lease"] = spec.lease_id
        now = time.time()
        entry = {
            "id": vm_name,
            "labels": labels,
            "created_at": now,
            "expires_at": now + spec.max_lifetime_s,
            "os": spec.os.value,
            "cpus": max(1, round(spec.resources.cpus)),
            "memory_mb": spec.resources.memory_mb,
            "template": spec.template.ref,
            "digest": spec.template.digest,
        }
        path = self._registry_path(vm_name)
        path.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")

    def _entry(self, vm_name: str) -> dict[str, Any]:
        path = self._registry_path(vm_name)
        if not path.exists():
            return {}
        return cast(Entry, json.loads(path.read_text(encoding="utf-8")))

    def _entries(self) -> EntryList:
        entries: EntryList = []
        for path in sorted(self._vms_dir.glob("*.json")):
            try:
                entries.append(cast(Entry, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        return entries

    def _macos_sandboxes(self) -> int:
        return sum(1 for entry in self._entries() if entry.get("os") == OSFamily.MACOS.value)

    async def _vm_names(self) -> set[str] | None:
        result = await self._run_tart("list", limit_s=60.0)
        if result.returncode != 0:
            return None
        names: set[str] = set()
        for raw in result.stdout.decode(errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            token = line.split()[0]
            if token.upper() in {"NAME", "SOURCE", "DISK", "SIZE", "STATE", "ACCESSED"}:
                continue
            names.add(token)
        return names

    # -- subprocess helpers -----------------------------------------------------------

    async def _run_tart(self, *args: str, limit_s: float) -> _Completed:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.tart_argv(*args),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SpleefError(f"cannot start tart: {exc}") from exc
        try:
            async with asyncio.timeout(limit_s):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise SpleefError(f"tart {' '.join(args)} timed out after {limit_s:g}s") from None
        return _Completed(process.returncode or 0, stdout, stderr)

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
                # The sweep is a safety net; a transient CLI error must not kill it.
                continue

    async def _sweep_once(self) -> None:
        now = time.time()
        for entry in self._entries():
            expires_at = float(entry.get("expires_at") or 0)
            if expires_at and expires_at <= now:
                await self._destroy_vm(str(entry.get("id", "")))


class _Completed:
    """The subset of ``subprocess.CompletedProcess`` this provider uses."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stderr(result: Any) -> str:
    return str(result.stderr.decode("utf-8", errors="replace")).strip()


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
            f"egress proxy address {address!r} is not a literal IP; tart softnet allowlists "
            "accept CIDRs only"
        ) from exc


async def _connect_ssh(target: Mapping[str, Any]) -> Any:
    """Default SSH transport: connect with asyncssh to the management account."""
    import asyncssh

    key = target.get("key")
    return await asyncssh.connect(
        str(target["host"]),
        port=int(target["port"]),
        username=str(target["username"]),
        client_keys=[str(key)] if key else None,
        known_hosts=target.get("known_hosts"),
    )
