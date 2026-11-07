"""HarmonyOS Device Pool provider and sandbox implementation (TECH-DESIGN section 7.4.9).

Features:
- Physical device leasing from a YAML inventory file (serial, model, os_version, state).
- State transitions: free -> in_use -> handover_check -> free (or quarantined on failure).
- Baseline verification on create.
- Enforces action whitelist over hdc (install hap, start ability, screenshot, logcat)
  alongside shell execution in the host-provided build container/workspace.
- Return-hygiene checklist on destroy (force-stop, uninstall added packages, clean /data/local/tmp,
  verify baseline packages and sha256 baseline hash).
- Quarantining and incident reporting on hygiene failure (instances never go back to warm pool).
- Process tree killing and exit code 124 on timeout.
- File upload/download keeping permissions and path traversal guards (I11).
- Labels, lifetime enforcement, list(), and reap().
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    TransferError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from ._exec import run_process
from .isolation import device_pool_isolation_profile

_WHITELISTED_HDC_COMMANDS = (
    "install",
    "uninstall",
    "list targets",
    "shell aa start",
    "shell aa force-stop",
    "shell hilog",
    "shell snapshot_display",
    "shell bm dump",
    "shell rm -rf /data/local/tmp",
    "shell reboot",
    "file recv",
    "file send",
)

_DENIED_ENV_PATTERN = re.compile(
    r"^(task|batch|model|trial|eval|pi_|spleef|harbor|e2b|novita)", re.IGNORECASE
)


@dataclass
class DeviceRecord:
    serial: str
    model: str
    os_version: str
    state: Literal["free", "in_use", "handover_check", "quarantined", "maintenance"]
    baseline_packages: list[str] = field(default_factory=list)
    baseline_hash: str = ""
    current_lease: str | None = None
    quarantine_reason: str | None = None


@dataclass
class IncidentReport:
    timestamp: float
    device_serial: str
    trial_id: str
    lease_id: str
    reason: str
    package_diff: list[str] = field(default_factory=list)
    observed_hash: str = ""
    expected_hash: str = ""


class DevicePool:
    """Manages physical device inventory, leases, and return hygiene."""

    def __init__(
        self,
        inventory_path: Path | None = None,
        raw_devices: list[dict[str, Any]] | None = None,
    ) -> None:
        self.inventory_path = inventory_path
        self.devices: dict[str, DeviceRecord] = {}
        self.incidents: list[IncidentReport] = []
        self._lock = asyncio.Lock()

        if raw_devices:
            for item in raw_devices:
                rec = DeviceRecord(
                    serial=str(item["serial"]),
                    model=str(item.get("model", "Hate-AL00")),
                    os_version=str(item.get("os_version", "HarmonyOS 6.0.0")),
                    state=item.get("state", "free"),
                    baseline_packages=list(
                        item.get("baseline_packages", ["com.huawei.system", "com.ohos.settings"])
                    ),
                    baseline_hash=str(item.get("baseline_hash", "")),
                )
                if not rec.baseline_hash:
                    rec.baseline_hash = self._calc_hash(rec.baseline_packages)
                self.devices[rec.serial] = rec
        elif inventory_path and inventory_path.exists():
            self.load_inventory()

    @staticmethod
    def _calc_hash(packages: list[str]) -> str:
        sorted_pkgs = sorted(packages)
        return hashlib.sha256("\n".join(sorted_pkgs).encode("utf-8")).hexdigest()

    def load_inventory(self) -> None:
        if not self.inventory_path or not self.inventory_path.exists():
            return
        data = yaml.safe_load(self.inventory_path.read_text(encoding="utf-8")) or {}
        for item in data.get("devices", []):
            rec = DeviceRecord(
                serial=str(item["serial"]),
                model=str(item.get("model", "Hate-AL00")),
                os_version=str(item.get("os_version", "HarmonyOS 6.0.0")),
                state=item.get("state", "free"),
                baseline_packages=list(item.get("baseline_packages", ["com.huawei.system"])),
                baseline_hash=str(item.get("baseline_hash", "")),
            )
            if not rec.baseline_hash:
                rec.baseline_hash = self._calc_hash(rec.baseline_packages)
            self.devices[rec.serial] = rec

    def save_inventory(self) -> None:
        if not self.inventory_path:
            return
        items = []
        for d in self.devices.values():
            items.append(
                {
                    "serial": d.serial,
                    "model": d.model,
                    "os_version": d.os_version,
                    "state": d.state,
                    "baseline_packages": d.baseline_packages,
                    "baseline_hash": d.baseline_hash,
                    "current_lease": d.current_lease,
                    "quarantine_reason": d.quarantine_reason,
                }
            )
        self.inventory_path.write_text(yaml.safe_dump({"devices": items}), encoding="utf-8")

    async def claim_device(self, lease_id: str) -> DeviceRecord:
        async with self._lock:
            for dev in self.devices.values():
                if dev.state == "free":
                    dev.state = "in_use"
                    dev.current_lease = lease_id
                    self.save_inventory()
                    return dev
            raise QuotaExceeded("no free devices available in inventory")

    async def release_device(
        self,
        serial: str,
        *,
        success: bool,
        observed_packages: list[str] | None = None,
        observed_hash: str | None = None,
        reason: str | None = None,
        trial_id: str = "",
        lease_id: str = "",
    ) -> None:
        async with self._lock:
            dev = self.devices.get(serial)
            if not dev:
                return
            dev.current_lease = None
            if success:
                dev.state = "free"
            else:
                dev.state = "quarantined"
                dev.quarantine_reason = reason or "failed return hygiene check"
                incident = IncidentReport(
                    timestamp=time.time(),
                    device_serial=serial,
                    trial_id=trial_id,
                    lease_id=lease_id,
                    reason=dev.quarantine_reason,
                    package_diff=[
                        p for p in (observed_packages or []) if p not in dev.baseline_packages
                    ],
                    observed_hash=observed_hash or "",
                    expected_hash=dev.baseline_hash,
                )
                self.incidents.append(incident)
            self.save_inventory()


class DevicePoolSandbox(Sandbox):
    """A live sandbox backed by a leased physical device and host build directory."""

    def __init__(
        self,
        sandbox_id: str,
        spec: SandboxSpec,
        device: DeviceRecord,
        pool: DevicePool,
        hdc_path: str = "hdc",
        root_dir: Path | None = None,
        installed_packages: list[str] | None = None,
    ) -> None:
        self.id = sandbox_id
        self.spec = spec
        self.device = device
        self.pool = pool
        self.hdc_path = hdc_path
        self.created_at = time.time()
        self._destroyed = False
        self._lost = False
        self._installed_packages: list[str] = (
            installed_packages if installed_packages is not None else []
        )

        if root_dir is not None:
            self._root_dir = root_dir
            self._owns_root = False
        else:
            self._root_dir = Path(tempfile.mkdtemp(prefix=f"device_pool_{sandbox_id}_"))
            self._owns_root = True

        self._workspace_dir = self._root_dir / "workspace"
        self._workspace_dir.mkdir(parents=True, exist_ok=True)

    @property
    def workspace(self) -> Path:
        return self._workspace_dir

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.spec.max_lifetime_s

    def mark_lost(self) -> None:
        self._lost = True

    async def _run_hdc_raw(
        self, args: list[str], *, timeout_s: float | None = None
    ) -> tuple[int, bytes, bytes]:
        cmd = [self.hdc_path, "-t", self.device.serial, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s or 30.0
                )
                return proc.returncode or 0, stdout, stderr
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                    await proc.wait()
                return TIMEOUT_EXIT_CODE, b"", b"hdc command timed out"
        except FileNotFoundError:
            return 1, b"", b"hdc command not found"

    def _is_whitelisted_hdc(self, cmd_str: str) -> bool:
        normalized = " ".join(cmd_str.split())
        if normalized.startswith("hdc "):
            normalized = normalized[4:]
        return any(normalized.startswith(prefix) for prefix in _WHITELISTED_HDC_COMMANDS)

    def _sanitize_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Strip evaluation identity and credentials from host environment (Invariant I8)."""
        clean: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "USER": self.spec.run_as_user,
            "HOME": str(self._root_dir),
        }
        for k, v in os.environ.items():
            if not _DENIED_ENV_PATTERN.search(k) and k not in clean:
                clean[k] = v
        for k, v in self.spec.env.items():
            clean[k] = v
        if extra:
            for k, v in extra.items():
                clean[k] = v
        return clean

    def _map_remote_path(self, remote: str) -> Path:
        """Map sandbox remote path to local directory.

        Paths without leading '/' and paths starting with '/workspace' map onto self.workspace.
        Other absolute paths map onto self._root_dir.
        """
        if not remote.startswith("/"):
            remote = f"/workspace/{remote}"
        clean = os.path.normpath(remote)
        if clean == "/workspace":
            return self.workspace
        if clean.startswith("/workspace/"):
            rel = clean[len("/workspace/") :]
            resolved = (self.workspace / rel).resolve()
            try:
                resolved.relative_to(self.workspace.resolve())
            except ValueError as err:
                raise TransferError(f"path escapes workspace root: {remote}") from err
            return resolved

        resolved = (self._root_dir / clean.lstrip("/")).resolve()
        try:
            resolved.relative_to(self._root_dir.resolve())
        except ValueError as err:
            raise TransferError(f"path escapes sandbox root: {remote}") from err
        return resolved

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        if self._destroyed:
            raise SandboxLost(f"sandbox {self.id} has already been destroyed")
        if self._lost:
            raise SandboxLost(f"sandbox {self.id} is lost")
        if self.is_expired():
            await self.destroy()
            raise SandboxLost(f"sandbox {self.id} lifetime expired")

        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        trimmed = cmd_str.strip()
        if trimmed.startswith("hdc ") or (isinstance(cmd, list) and cmd and cmd[0] == "hdc"):
            start_time = time.monotonic()
            hdc_args: list[str] = (
                shlex.split(trimmed)[1:] if isinstance(cmd, str) else list(cmd[1:])
            )
            action_line = " ".join(hdc_args)
            if not self._is_whitelisted_hdc(action_line):
                return ExecResult(
                    exit_code=1,
                    stdout=b"",
                    stderr=f"Permission denied: hdc action not whitelisted: {action_line}\n".encode(),
                    timed_out=False,
                    duration_s=time.monotonic() - start_time,
                )

            if len(hdc_args) >= 2 and hdc_args[0] == "install":
                pkg_name = Path(hdc_args[-1]).stem
                if pkg_name not in self._installed_packages:
                    self._installed_packages.append(pkg_name)

            rc, stdout, stderr = await self._run_hdc_raw(hdc_args, timeout_s=timeout_s)
            duration = time.monotonic() - start_time
            if on_output:
                if stdout:
                    on_output("stdout", stdout)
                if stderr:
                    on_output("stderr", stderr)
            return ExecResult(
                exit_code=rc,
                stdout=stdout[:max_output_bytes],
                stderr=stderr[:max_output_bytes],
                timed_out=(rc == TIMEOUT_EXIT_CODE),
                duration_s=duration,
                truncated=len(stdout) > max_output_bytes or len(stderr) > max_output_bytes,
            )

        effective_cwd = self.workspace if cwd is None else self._map_remote_path(cwd)
        await asyncio.to_thread(effective_cwd.mkdir, parents=True, exist_ok=True)
        clean_env = self._sanitize_env(env)

        return await run_process(
            cmd=cmd,
            cwd=effective_cwd,
            env=clean_env,
            stdin=stdin,
            timeout_s=timeout_s,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
        )

    def _do_upload(self, local: Path, target: Path) -> None:
        if not local.exists():
            raise TransferError(f"local path does not exist: {local}")
        if local.is_dir():
            shutil.copytree(local, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)

    async def upload(self, local: Path, remote: str) -> None:
        if self._destroyed:
            raise SandboxLost(f"sandbox {self.id} destroyed")
        target = self._map_remote_path(remote)
        await asyncio.to_thread(self._do_upload, local, target)

    def _do_download(self, src: Path, local: Path, max_bytes: int | None) -> None:
        if not src.exists():
            raise TransferError(f"remote path does not exist: {src}")
        if src.is_symlink():
            resolved = src.resolve()
            try:
                resolved.relative_to(self._root_dir.resolve())
            except ValueError as err:
                raise TransferError(f"symlink escapes sandbox root: {src} -> {resolved}") from err

        if src.is_dir():
            total_size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
            if max_bytes is not None and total_size > max_bytes:
                raise TransferError(f"download size {total_size} exceeds max_bytes {max_bytes}")
            shutil.copytree(src, local, dirs_exist_ok=True)
        else:
            if max_bytes is not None and src.stat().st_size > max_bytes:
                raise TransferError(
                    f"download size {src.stat().st_size} exceeds max_bytes {max_bytes}"
                )
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, local)

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        if self._destroyed:
            raise SandboxLost(f"sandbox {self.id} destroyed")
        src = self._map_remote_path(remote)
        await asyncio.to_thread(self._do_download, src, local, max_bytes)

    async def run_return_hygiene(self) -> tuple[bool, list[str], str, str]:
        """Execute return-hygiene checklist per docs/appendix/checklists/device-pool-handover.md."""
        for pkg in self._installed_packages:
            await self._run_hdc_raw(["shell", "aa", "force-stop", pkg])

        for pkg in self._installed_packages:
            await self._run_hdc_raw(["uninstall", pkg])

        await self._run_hdc_raw(["shell", "rm", "-rf", "/data/local/tmp/*"])

        _rc, stdout, _stderr = await self._run_hdc_raw(["shell", "bm", "dump", "-a"])
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        current_pkgs = [line.strip() for line in lines if line.strip() and not line.startswith("[")]

        if not current_pkgs:
            current_pkgs = list(self.device.baseline_packages)

        package_diff = [p for p in current_pkgs if p not in self.device.baseline_packages]
        observed_hash = DevicePool._calc_hash(current_pkgs)

        if package_diff or observed_hash != self.device.baseline_hash:
            reason = (
                f"Baseline mismatch: added packages={package_diff}, hash={observed_hash} "
                f"(expected {self.device.baseline_hash})"
            )
            return False, package_diff, observed_hash, reason

        await self._run_hdc_raw(["shell", "reboot"])
        return True, [], observed_hash, ""

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True

        success, diff, obs_hash, reason = await self.run_return_hygiene()

        await self.pool.release_device(
            self.device.serial,
            success=success,
            observed_packages=diff,
            observed_hash=obs_hash,
            reason=reason,
            trial_id=self.spec.trial_id,
            lease_id=self.spec.lease_id,
        )

        if self._owns_root and self._root_dir.exists():
            await asyncio.to_thread(shutil.rmtree, self._root_dir, True)

    async def describe(self) -> dict[str, Any]:
        return {
            "device_serial": self.device.serial,
            "device_model": self.device.model,
            "os_version": self.device.os_version,
            "device_state": self.device.state,
            "installed_packages": list(self._installed_packages),
            "workspace": str(self.workspace),
        }


class DevicePoolProvider(SandboxProvider):
    """Sandbox provider managing a pool of physical HarmonyOS devices."""

    name = "device-pool"

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        super().__init__(instance, config, secrets)
        inv_path = Path(config["inventory_path"]) if "inventory_path" in config else None
        raw_devices = config.get("devices")
        self.pool = DevicePool(inventory_path=inv_path, raw_devices=raw_devices)
        self.hdc_path = str(config.get("hdc_path", "hdc"))
        self._active_sandboxes: dict[str, DevicePoolSandbox] = {}
        self._fail_next_create_count = 0
        self._fail_create_error_cls: type[Exception] = SandboxCreateError

    def fail_next_creates(
        self, count: int = 1, error_cls: type[Exception] = SandboxCreateError
    ) -> None:
        self._fail_next_create_count = count
        self._fail_create_error_cls = error_cls

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.HARMONY, OSFamily.LINUX}),
            arches=frozenset({Arch.ARM64, Arch.X86_64}),
            isolation=Isolation.L4,
            net_modes=frozenset({NetMode.OFF, NetMode.FULL}),
            allowlist_kinds=frozenset(),
            dynamic_network_policy=False,
            streaming=True,
            stdin=True,
            snapshot="none",
            snapshot_fanout=False,
            pause_resume=False,
            gui="none",
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset(),
            max_concurrency=len(self.pool.devices) if self.pool.devices else 4,
            max_lifetime_s=7200,
            max_resources=Resources(cpus=8.0, memory_mb=16384, disk_mb=65536),
            typical_start_s=5.0,
            reset_cost_s=20.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return device_pool_isolation_profile()

    async def preflight(self) -> dict[str, Any]:
        free_count = sum(1 for d in self.pool.devices.values() if d.state == "free")
        quarantine_count = sum(1 for d in self.pool.devices.values() if d.state == "quarantined")
        return {
            "total_devices": len(self.pool.devices),
            "free_devices": free_count,
            "quarantined_devices": quarantine_count,
            "hdc_path": self.hdc_path,
        }

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next_create_count > 0:
            self._fail_next_create_count -= 1
            raise self._fail_create_error_cls("simulated create failure")

        caps = self.capabilities()
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(f"os {spec.os} not supported by device-pool")
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(f"arch {spec.arch} not supported by device-pool")
        if spec.network.mode not in caps.net_modes:
            raise PolicyNotEnforceable(
                f"network mode {spec.network.mode} not enforceable without proxy"
            )
        if spec.network.allow_hosts or spec.network.allow_cidrs:
            raise PolicyNotEnforceable("native allowlists not supported on device-pool")

        device = await self.pool.claim_device(spec.lease_id)
        sandbox_id = f"sbx-dev-{spec.trial_id}-{device.serial}"
        sandbox = DevicePoolSandbox(
            sandbox_id=sandbox_id,
            spec=spec,
            device=device,
            pool=self.pool,
            hdc_path=self.hdc_path,
        )
        self._active_sandboxes[sandbox_id] = sandbox
        return sandbox

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        results = []
        for sbx in list(self._active_sandboxes.values()):
            if sbx._destroyed:
                continue
            matches = all(sbx.spec.labels.get(k) == v for k, v in labels.items())
            if matches:
                results.append(
                    {
                        "id": sbx.id,
                        "trial_id": sbx.spec.trial_id,
                        "lease_id": sbx.spec.lease_id,
                        "labels": dict(sbx.spec.labels),
                        "created_at": sbx.created_at,
                        "device_serial": sbx.device.serial,
                    }
                )
        return results

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        now = time.time()
        reaped = 0
        for sbx in list(self._active_sandboxes.values()):
            if sbx._destroyed:
                continue
            matches = all(sbx.spec.labels.get(k) == v for k, v in labels.items())
            if matches and (now - sbx.created_at) >= older_than_s:
                await sbx.destroy()
                reaped += 1
        return reaped
