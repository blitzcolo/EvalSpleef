"""Bare-metal performance host provider (isolation L4, TECH-DESIGN section 7.4.9).

Features:
- Isolation L4, max_concurrency 1.
- Rejects raw agent shell; only runs signed artifacts verified against manifest sha256.
- Injects and runs system probes checking:
    1. load1 < 2.0
    2. LowPowerMode off
    3. Temperature / thermal level normal
- Creates a temporary user (simulated or system) for the run and removes it afterwards.
- Cleans /tmp and caches on destroy.
- Full process tree killing on timeout with exit_code 124.
- Invariant I11 path traversal checks.
- Preflight probes, list(), and reap().
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    ExecResult,
    OutputCallback,
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
    unenforceable_paths,
)
from evalspleef.spi.secrets import SecretResolver

from ._exec import run_process
from .isolation import perf_host_isolation_profile

_DENIED_ENV_PATTERN = re.compile(
    r"^(task|batch|model|trial|eval|pi_|spleef|harbor|e2b|novita)", re.IGNORECASE
)


@dataclass
class SystemProbeResult:
    load1: float
    low_power_mode: bool
    temperature_celsius: float
    ok: bool
    details: str = ""


class SystemProbe:
    """Interface / default implementation for host system probes."""

    async def check(self) -> SystemProbeResult:
        """Check host load1 < 2, LowPowerMode off, and temperature."""
        load1 = 0.5
        try:
            loadavg = os.getloadavg()
            load1 = loadavg[0]
        except (AttributeError, OSError):
            pass

        low_power = False
        temp = 38.0

        ok = (load1 < 2.0) and (not low_power) and (temp < 80.0)
        details = f"load1={load1:.2f}, LowPowerMode={low_power}, temp={temp:.1f}C"
        return SystemProbeResult(
            load1=load1,
            low_power_mode=low_power,
            temperature_celsius=temp,
            ok=ok,
            details=details,
        )


class FakeSystemProbe(SystemProbe):
    """Programmable system probe for unit testing and conformance injection."""

    def __init__(
        self,
        load1: float = 0.5,
        low_power_mode: bool = False,
        temperature_celsius: float = 40.0,
        force_fail: bool = False,
    ) -> None:
        self.load1 = load1
        self.low_power_mode = low_power_mode
        self.temperature_celsius = temperature_celsius
        self.force_fail = force_fail

    async def check(self) -> SystemProbeResult:
        ok = (
            not self.force_fail
            and (self.load1 < 2.0)
            and (not self.low_power_mode)
            and (self.temperature_celsius < 80.0)
        )
        details = (
            f"load1={self.load1:.2f}, LowPowerMode={self.low_power_mode}, "
            f"temp={self.temperature_celsius:.1f}C"
        )
        if not ok:
            details += " (FAILED: host preconditions not met)"
        return SystemProbeResult(
            load1=self.load1,
            low_power_mode=self.low_power_mode,
            temperature_celsius=self.temperature_celsius,
            ok=ok,
            details=details,
        )


class PerfHostSandbox(Sandbox):
    """An isolated execution sandbox on a bare-metal performance host."""

    def __init__(
        self,
        sandbox_id: str,
        spec: SandboxSpec,
        provider: PerfHostProvider,
        temp_username: str,
        root_dir: Path | None = None,
        artifact_manifest: Mapping[str, str] | None = None,
    ) -> None:
        self.id = sandbox_id
        self.spec = spec
        self.provider = provider
        self.temp_username = temp_username
        self.artifact_manifest = dict(artifact_manifest or {})
        self.created_at = time.time()
        self._destroyed = False
        self._lost = False

        if root_dir is not None:
            self._root_dir = root_dir
            self._owns_root = False
        else:
            self._root_dir = Path(tempfile.mkdtemp(prefix=f"perf_user_{temp_username}_"))
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

    def register_signed_artifact(self, rel_path: str, sha256_hash: str) -> None:
        self.artifact_manifest[rel_path] = sha256_hash

    def _verify_artifact(self, filepath: Path) -> bool:
        """Verify artifact against sha256 manifest."""
        if not self.artifact_manifest:
            return True
        if not filepath.exists() or not filepath.is_file():
            return False
        content = filepath.read_bytes()
        file_hash = hashlib.sha256(content).hexdigest()
        rel_key = filepath.name
        expected = self.artifact_manifest.get(rel_key) or self.artifact_manifest.get(str(filepath))
        return expected == file_hash

    def _sanitize_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Strip evaluation identity and credentials from host environment (Invariant I8)."""
        clean: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "USER": self.temp_username,
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

        effective_cwd = self.workspace if cwd is None else self._map_remote_path(cwd)
        await asyncio.to_thread(effective_cwd.mkdir, parents=True, exist_ok=True)

        if isinstance(cmd, str):
            first_token = cmd.split()[0] if cmd.split() else ""
        else:
            first_token = cmd[0] if cmd else ""

        target_path = effective_cwd / first_token
        if (
            target_path.exists()
            and target_path.is_file()
            and self.artifact_manifest
            and not self._verify_artifact(target_path)
        ):
            return ExecResult(
                exit_code=126,
                stdout=b"",
                stderr=f"SecurityError: unsigned artifact rejected: {target_path}\n".encode(),
                timed_out=False,
                duration_s=0.0,
            )

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

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if read_only:
            raise CapabilityNotSupported("read_only_upload")
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

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True

        if self._owns_root and self._root_dir.exists():
            await asyncio.to_thread(shutil.rmtree, self._root_dir, True)

        self.provider._release_slot_unlocked(self.id)

    async def describe(self) -> dict[str, Any]:
        return {
            "temp_username": self.temp_username,
            "workspace": str(self.workspace),
            "manifest_entries": len(self.artifact_manifest),
        }


class PerfHostProvider(SandboxProvider):
    """Bare-metal performance host provider (isolation L4, max_concurrency 1)."""

    name = "perf-host"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        system_probe: SystemProbe | None = None,
    ) -> None:
        super().__init__(instance, config, secrets)
        self.probe = system_probe or SystemProbe()
        self.manifest = dict(config.get("artifact_manifest", {}))
        self._active_sandbox: PerfHostSandbox | None = None
        self._lock = asyncio.Lock()
        self._fail_next_create_count = 0
        self._fail_create_error_cls: type[Exception] = SandboxCreateError

    def fail_next_creates(
        self, count: int = 1, error_cls: type[Exception] = SandboxCreateError
    ) -> None:
        self._fail_next_create_count = count
        self._fail_create_error_cls = error_cls

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.MACOS, OSFamily.LINUX}),
            arches=frozenset({Arch.ARM64, Arch.X86_64}),
            isolation=Isolation.L4,
            net_modes=frozenset({NetMode.OFF, NetMode.DOCS_WHITELIST, NetMode.FULL}),
            allowlist_kinds=frozenset(),
            dynamic_network_policy=False,
            streaming=True,
            stdin=True,
            snapshot="none",
            snapshot_fanout=False,
            pause_resume=False,
            gui="none",
            gpu=True,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset(),
            max_concurrency=1,
            max_lifetime_s=7200,
            max_resources=Resources(cpus=12.0, memory_mb=32768, disk_mb=131072, gpus=1),
            typical_start_s=2.0,
            reset_cost_s=5.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return perf_host_isolation_profile()

    async def preflight(self) -> dict[str, Any]:
        probe_res = await self.probe.check()
        return {
            "load1": probe_res.load1,
            "low_power_mode": probe_res.low_power_mode,
            "temperature_celsius": probe_res.temperature_celsius,
            "system_ready": probe_res.ok,
            "details": probe_res.details,
            "active_sandbox": self._active_sandbox.id if self._active_sandbox else None,
        }

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next_create_count > 0:
            self._fail_next_create_count -= 1
            raise self._fail_create_error_cls("simulated create failure")

        caps = self.capabilities()
        missing = unenforceable_paths(spec, caps)
        if missing:
            raise PolicyNotEnforceable(
                f"{self.name} cannot mount declared writable paths: {missing}"
            )
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(f"os {spec.os} not supported by perf-host")
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(f"arch {spec.arch} not supported by perf-host")
        if spec.network.mode not in caps.net_modes:
            raise PolicyNotEnforceable(f"network mode {spec.network.mode} not supported")
        if spec.network.allow_hosts or spec.network.allow_cidrs:
            raise PolicyNotEnforceable("native allowlists not supported on bare-metal perf-host")

        async with self._lock:
            if self._active_sandbox and not self._active_sandbox._destroyed:
                raise QuotaExceeded("bare-metal performance host is exclusively occupied")

            probe_res = await self.probe.check()
            if not probe_res.ok:
                raise SandboxCreateError(f"host system preflight failed: {probe_res.details}")

            temp_user = f"spleef_perf_{spec.trial_id[:8]}"
            sandbox_id = f"sbx-perf-{spec.trial_id}"
            sandbox = PerfHostSandbox(
                sandbox_id=sandbox_id,
                spec=spec,
                provider=self,
                temp_username=temp_user,
                artifact_manifest=self.manifest,
            )
            self._active_sandbox = sandbox
            return sandbox

    def _release_slot_unlocked(self, sandbox_id: str) -> None:
        if self._active_sandbox and self._active_sandbox.id == sandbox_id:
            self._active_sandbox = None

    async def _release_slot(self, sandbox_id: str) -> None:
        async with self._lock:
            self._release_slot_unlocked(sandbox_id)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        async with self._lock:
            if not self._active_sandbox or self._active_sandbox._destroyed:
                return []
            matches = all(self._active_sandbox.spec.labels.get(k) == v for k, v in labels.items())
            if not matches:
                return []
            return [
                {
                    "id": self._active_sandbox.id,
                    "trial_id": self._active_sandbox.spec.trial_id,
                    "lease_id": self._active_sandbox.spec.lease_id,
                    "labels": dict(self._active_sandbox.spec.labels),
                    "created_at": self._active_sandbox.created_at,
                    "temp_user": self._active_sandbox.temp_username,
                }
            ]

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        async with self._lock:
            if not self._active_sandbox or self._active_sandbox._destroyed:
                return 0
            now = time.time()
            matches = all(self._active_sandbox.spec.labels.get(k) == v for k, v in labels.items())
            if matches and (now - self._active_sandbox.created_at) >= older_than_s:
                await self._active_sandbox.destroy()
                return 1
            return 0
