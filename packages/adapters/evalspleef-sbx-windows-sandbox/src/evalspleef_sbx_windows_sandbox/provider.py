"""Windows Sandbox provider (TECH-DESIGN section 7.4.7, PRD FR-SBX-08).

Windows Sandbox is a developer self-test carrier only. The host runs one
instance at a time, ``wsb exec`` returns nothing but an exit code and the
network can only be fully on or fully off. The provider therefore declares
``max_concurrency=1``, ``streaming=False``, no allowlist kinds and advertises
isolation L3 with a bounded instance lifetime.

``dev_only = True`` marks the adapter as unsuitable for a formal batch: the
control plane must keep it out of batch routes (this package documents that;
no route configuration lives here).

Command contract emulated by ``fake_wsb``:

``wsb start --config <xml>`` -> instance id on stdout
``wsb exec --id <id> -c <cmd> -r ExistingLogin`` -> exit code only
``wsb stop --id <id>``

The config XML maps one host folder into the guest and enables or disables
networking; that is the only knob the provider has.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SpleefError,
    TransferError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from .isolation import isolation_profile, windows_probe_script
from .sandbox import OUTPUT_DIR, WindowsSandbox

PROVIDER_LABEL = "spleef.provider"
EXPIRES_LABEL = "spleef.expires_at"

_DEFAULT_GUEST_WORKSPACE = r"C:\Users\WDAGUtilityAccount\Desktop\workspace"
_GUEST_OS = {"windows": OSFamily.WINDOWS, "linux": OSFamily.LINUX}
_PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC")
_PROBE_LINE = re.compile(r"^CHECK (\S+) (PASS|FAIL) ?(.*)$")


class WindowsSandboxProvider(SandboxProvider):
    """Sandbox provider backed by a local ``wsb`` CLI on a Windows host."""

    name = "windows-sandbox"
    dev_only: bool = True

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._wsb = str(config.get("wsb_path", "wsb"))
        state_dir = config.get("state_dir")
        self._state_dir = (
            Path(str(state_dir))
            if state_dir is not None
            else Path(tempfile.gettempdir()) / f"evalspleef-wsb-{instance}"
        )
        guest_os = str(config.get("guest_os", "windows"))
        if guest_os not in _GUEST_OS:
            raise ValueError(f"guest_os must be one of {sorted(_GUEST_OS)}, got {guest_os!r}")
        self._guest_os = _GUEST_OS[guest_os]
        self._guest_workspace = str(config.get("guest_workspace", _DEFAULT_GUEST_WORKSPACE))
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 1800)
        self._exec_timeout_s = _config_float(config, "exec_timeout_s", 600.0)
        self._start_timeout_s = _config_float(config, "start_timeout_s", 120.0)
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    # -- configuration surfaces ---------------------------------------------------

    @property
    def guest_os(self) -> OSFamily:
        return self._guest_os

    @property
    def guest_workspace(self) -> str:
        return self._guest_workspace

    @property
    def exec_timeout_s(self) -> float:
        return self._exec_timeout_s

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({self._guest_os}),
            arches=frozenset({Arch.X86_64}),
            isolation=Isolation.L3,
            net_modes=frozenset({NetMode.OFF, NetMode.FULL}),
            allowlist_kinds=frozenset(),
            dynamic_network_policy=False,
            streaming=False,
            stdin=False,
            snapshot="none",
            snapshot_fanout=False,
            pause_resume=False,
            gui="none",
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset(),
            max_concurrency=1,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=20.0,
            reset_cost_s=30.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile(Isolation.L3)

    # -- host helpers ---------------------------------------------------------------

    @property
    def _instances_dir(self) -> Path:
        return self._state_dir / "instances"

    def entry(self, instance_id: str) -> dict[str, Any] | None:
        """The metadata of a live instance, or ``None`` when it is gone."""
        path = self._instances_dir / f"{instance_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _child_env(self, workspace: Path | None) -> dict[str, str]:
        env = {key: os.environ[key] for key in _PASSTHROUGH_ENV if key in os.environ}
        env["SPLEEF_WSB_STATE"] = str(self._state_dir / "fake")
        if workspace is not None and self._guest_os is OSFamily.LINUX:
            # The fake guest is a POSIX process: a neutral HOME keeps host
            # credentials and host mounts out of the command's view.
            env["HOME"] = str(workspace)
        return env

    async def run_wsb(
        self,
        args: list[str],
        *,
        timeout_s: float,
        workspace: Path | None = None,
    ) -> tuple[int, bytes, bytes, bool]:
        """Run the ``wsb`` CLI; the timeout kills the whole client process tree."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._wsb,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(workspace),
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxCreateError(f"cannot start {self._wsb!r}: {exc}") from exc
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await self._kill_tree(process)
            stdout = b""
            stderr = b""
            if process.stdout is not None:
                stdout = await process.stdout.read()
            if process.stderr is not None:
                stderr = await process.stderr.read()
        return process.returncode or 0, stdout, stderr, timed_out

    @staticmethod
    async def _kill_tree(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10.0)

    def host_path(self, remote: str, workspace: Path) -> Path:
        """Map a sandbox path (``/workspace/...``) to the host mapped folder."""
        relative = _workspace_relative(remote)
        mapped = workspace.joinpath(*relative.parts) if relative.parts else workspace
        root = str(workspace.resolve())
        resolved = os.path.realpath(mapped)
        if os.path.commonpath([root, resolved]) != root:
            raise TransferError(f"path escapes the mapped workspace: {remote!r}")
        return mapped

    def guest_path(self, remote: str, workspace: Path) -> str:
        """The path a command inside the guest uses for a sandbox path."""
        if self._guest_os is OSFamily.LINUX:
            return str(self.host_path(remote, workspace))
        relative = _workspace_relative(remote)
        base = PureWindowsPath(self._guest_workspace)
        return str(base.joinpath(*relative.parts) if relative.parts else base)

    # -- preflight --------------------------------------------------------------

    async def preflight(self) -> dict[str, Any]:
        executable = _resolve_executable(self._wsb)
        if executable is None:
            raise SpleefError(f"wsb executable not found: {self._wsb!r}")
        returncode, stdout, stderr, timed_out = await self.run_wsb(["--version"], timeout_s=20.0)
        return {
            "provider": f"{self.name}/{self.instance}",
            "executable": executable,
            "cli_ok": returncode == 0 and not timed_out,
            "version": stdout.decode("utf-8", "replace").strip() if returncode == 0 else "",
            "stderr": stderr.decode("utf-8", "replace").strip(),
            "dev_only": self.dev_only,
            "guest_os": self._guest_os.value,
            "state_dir": str(self._state_dir),
            "max_concurrency": 1,
            "live_instances": len(await self.list({})),
        }

    async def run_guest_probes(self, sandbox: Sandbox) -> dict[str, str]:
        """Run the shipped Windows probe script through ``exec``.

        Returns one ``{"I<n>": "PASS: ..."/"FAIL: ..."}`` entry per checked
        invariant. Only meaningful on a real Windows guest.
        """
        result = await sandbox.exec(windows_probe_script(), timeout_s=120.0)
        checks: dict[str, str] = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            match = _PROBE_LINE.match(line.strip())
            if match is not None:
                checks[match.group(1)] = f"{match.group(2)}: {match.group(3)}".strip()
        return checks

    # -- test-side controls (used by the conformance injectors) -------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        self._fail_next.extend([error] * n)

    async def lose(self, instance_id: str) -> None:
        """Simulate the host reclaiming an instance."""
        await self.release_sandbox(instance_id)

    async def expire_live_sandboxes(self) -> None:
        for entry in await self.list({}):
            await self.release_sandbox(str(entry["id"]))

    async def release_sandbox(self, instance_id: str) -> None:
        """Stop an instance and drop its metadata; a missing instance counts as success."""
        entry = self.entry(instance_id)
        # A missing wsb binary must not keep the metadata of a dead instance.
        with contextlib.suppress(SpleefError):
            await self.run_wsb(
                ["stop", "--id", instance_id],
                timeout_s=60.0,
                workspace=None,
            )
        (self._instances_dir / f"{instance_id}.json").unlink(missing_ok=True)
        if entry is not None:
            workspace = Path(str(entry.get("workspace", "")))
            if workspace.name == "workspace":
                shutil.rmtree(workspace.parent, ignore_errors=True)

    # -- SandboxProvider ------------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        self._validate_spec(spec)
        if self._pending:
            raise QuotaExceeded(
                f"windows-sandbox {self.instance!r} allows one instance per host",
                detail={"provider": self.instance, "max_concurrency": 1},
            )
        self._pending = 1
        try:
            live = await self.list({})
            if live:
                raise QuotaExceeded(
                    f"windows-sandbox {self.instance!r} already runs instance {live[0]['id']!r}",
                    detail={"provider": self.instance, "max_concurrency": 1},
                )
            return await self._create_instance(spec)
        finally:
            self._pending = 0

    async def _create_instance(self, spec: SandboxSpec) -> WindowsSandbox:
        token = uuid.uuid4().hex
        workspace = self._state_dir / "workspaces" / token / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / OUTPUT_DIR).mkdir(exist_ok=True)
        config_path = self._state_dir / "configs" / f"{token}.wsb"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(self._config_xml(spec, workspace), encoding="utf-8")

        returncode, stdout, stderr, timed_out = await self.run_wsb(
            ["start", "--config", str(config_path)],
            timeout_s=self._start_timeout_s,
            workspace=None,
        )
        if timed_out or returncode != 0:
            config_path.unlink(missing_ok=True)
            shutil.rmtree(workspace.parent, ignore_errors=True)
            raise SandboxCreateError(
                f"wsb start failed (exit {returncode}, timed_out={timed_out}): "
                f"{stderr.decode('utf-8', 'replace').strip()[:400]}",
                detail={"provider": self.instance},
            )
        instance_id = stdout.decode("utf-8", "replace").strip().splitlines()
        if not instance_id or not instance_id[-1]:
            config_path.unlink(missing_ok=True)
            shutil.rmtree(workspace.parent, ignore_errors=True)
            raise SandboxCreateError(
                "wsb start returned no instance id",
                detail={"provider": self.instance},
            )
        self._record(instance_id[-1].strip(), spec, workspace)
        config_path.unlink(missing_ok=True)
        return WindowsSandbox(self, instance_id[-1].strip(), spec, workspace)

    def _record(self, instance_id: str, spec: SandboxSpec, workspace: Path) -> None:
        now = time.time()
        lifetime = max(1, min(spec.max_lifetime_s, self._max_lifetime_s))
        labels = dict(spec.labels)
        labels[PROVIDER_LABEL] = self.instance
        labels[EXPIRES_LABEL] = str(int(now) + lifetime)
        metadata = {
            "id": instance_id,
            "labels": labels,
            "created_at": now,
            "expires_at": float(labels[EXPIRES_LABEL]),
            "workspace": str(workspace),
            "guest_os": self._guest_os.value,
            "network": spec.network.mode.value,
            "template": {
                "kind": spec.template.kind,
                "ref": spec.template.ref,
                "digest": spec.template.digest,
            },
        }
        self._instances_dir.mkdir(parents=True, exist_ok=True)
        (self._instances_dir / f"{instance_id}.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not self._instances_dir.is_dir():
            return entries
        for path in sorted(self._instances_dir.glob("*.json")):
            entry = self.entry(path.stem)
            if entry is None:
                continue
            entry_labels = entry.get("labels") or {}
            if not isinstance(entry_labels, dict):
                continue
            if not all(entry_labels.get(key) == value for key, value in labels.items()):
                continue
            entries.append(
                {
                    "id": str(entry.get("id", path.stem)),
                    "labels": dict(entry_labels),
                    "state": "running",
                    "created_at": float(entry.get("created_at") or 0),
                    "expires_at": float(entry.get("expires_at") or 0),
                }
            )
        return entries

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        now = time.time()
        reaped = 0
        for entry in await self.list(labels):
            past_age = (now - float(entry["created_at"])) >= older_than_s
            past_ttl = float(entry["expires_at"]) > 0 and float(entry["expires_at"]) <= now
            if not (past_age or past_ttl):
                continue
            await self.release_sandbox(str(entry["id"]))
            reaped += 1
        return reaped

    # -- validation and config XML ---------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> None:
        capabilities = self.capabilities()
        if spec.os not in capabilities.os_families:
            raise CapabilityNotSupported(
                f"windows-sandbox guest os is {self._guest_os.value!r}, got {spec.os!r}"
            )
        if spec.arch not in capabilities.arches:
            raise CapabilityNotSupported(f"windows-sandbox supports x86_64 only, got {spec.arch!r}")
        if spec.template.kind != "vhdx":
            raise CapabilityNotSupported(
                f"windows-sandbox needs a vhdx template, got {spec.template.kind!r}"
            )
        if spec.gui:
            raise CapabilityNotSupported("windows-sandbox does not expose a GUI endpoint")
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("windows-sandbox does not support GPUs")
        policy = spec.network
        if policy.via_proxy is not None:
            raise PolicyNotEnforceable(
                "windows-sandbox has no egress proxy hook; via_proxy cannot be honoured"
            )
        if policy.allow_hosts or policy.allow_cidrs:
            raise PolicyNotEnforceable(
                "windows-sandbox only switches networking fully off or fully on; "
                "allowlists cannot be enforced"
            )
        if policy.mode not in capabilities.net_modes:
            raise PolicyNotEnforceable(
                f"network mode {policy.mode.value!r} cannot be enforced; only 'off' and "
                "'full' exist"
            )

    def _config_xml(self, spec: SandboxSpec, workspace: Path) -> str:
        networking = "Disable" if spec.network.mode is NetMode.OFF else "Enable"
        sandbox_folder = (
            self._guest_workspace if self._guest_os is not OSFamily.LINUX else str(workspace)
        )
        return (
            "<Configuration>\n"
            f"  <Networking>{networking}</Networking>\n"
            f"  <MemoryInMB>{max(1, spec.resources.memory_mb)}</MemoryInMB>\n"
            "  <MappedFolders>\n"
            "    <MappedFolder>\n"
            f"      <HostFolder>{_xml_escape(str(workspace))}</HostFolder>\n"
            f"      <SandboxFolder>{_xml_escape(sandbox_folder)}</SandboxFolder>\n"
            "      <ReadOnly>false</ReadOnly>\n"
            "    </MappedFolder>\n"
            "  </MappedFolders>\n"
            "</Configuration>\n"
        )


def _resolve_executable(command: str) -> str | None:
    """The executable path for ``command``, or ``None`` when it does not exist."""
    if Path(command).is_file():
        return command
    return shutil.which(command)


def _workspace_relative(remote: str) -> PurePosixPath:
    """The path under ``/workspace`` for a sandbox path, rejecting escapes."""
    pure = PurePosixPath(remote.replace("\\", "/"))
    if not pure.is_absolute():
        raise TransferError(f"remote path must be absolute, got {remote!r}")
    parts = pure.parts[1:]
    if not parts or parts[0] != "workspace":
        raise TransferError(f"remote path must live under /workspace, got {remote!r}")
    relative = PurePosixPath(*parts[1:])
    if any(part == ".." for part in relative.parts):
        raise TransferError(f"remote path escapes /workspace: {remote!r}")
    return relative


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_float(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["EXPIRES_LABEL", "PROVIDER_LABEL", "WindowsSandboxProvider"]
