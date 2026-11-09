"""Sandbox SPI (TECH-DESIGN section 7.1).

The mandatory surface is create / exec / upload / download / destroy. Everything else is a
capability: callers read ``SandboxCapabilities`` first and the default implementations here
raise ``CapabilityNotSupported``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .caps import AllowKind, Arch, Isolation, NetMode, OSFamily
from .environment import ExecutionEnvironment
from .errors import CapabilityNotSupported
from .isolation import IsolationProfile
from .secrets import SecretResolver


@dataclass(frozen=True)
class NetworkPolicy:
    mode: NetMode
    allow_hosts: tuple[str, ...] = ()
    allow_cidrs: tuple[str, ...] = ()
    via_proxy: str | None = None
    """Egress proxy address. When set the sandbox may reach only this address."""


@dataclass(frozen=True)
class Resources:
    cpus: float
    memory_mb: int
    disk_mb: int
    gpus: int = 0
    gpu_type: str | None = None


TemplateKind = Literal[
    "oci", "novita_template", "e2b_template", "tart_image", "vhdx", "qcow2", "device_profile"
]


@dataclass(frozen=True)
class TemplateRef:
    kind: TemplateKind
    ref: str
    digest: str
    """sha256:... of the template; the Template Builder records the per-provider mapping."""


SandboxRole = Literal["agent", "verifier", "judge"]


@dataclass(frozen=True)
class SandboxSpec:
    trial_id: str
    lease_id: str
    role: SandboxRole
    os: OSFamily
    arch: Arch
    template: TemplateRef
    resources: Resources
    network: NetworkPolicy
    run_as_user: str = "agent"
    env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    """Must carry batch / trial / lease / provider so orphans can be found and reaped."""
    max_lifetime_s: int = 3600
    gui: bool = False


SnapshotSupport = Literal["none", "fs", "memory"]
GuiSupport = Literal["none", "vnc", "rdp"]
EnforcedLimit = Literal["cpu", "memory", "disk", "pids"]


@dataclass(frozen=True)
class SandboxCapabilities:
    os_families: frozenset[OSFamily]
    arches: frozenset[Arch]
    isolation: Isolation
    net_modes: frozenset[NetMode]
    allowlist_kinds: frozenset[AllowKind]
    dynamic_network_policy: bool
    streaming: bool
    stdin: bool
    snapshot: SnapshotSupport
    snapshot_fanout: bool
    pause_resume: bool
    gui: GuiSupport
    gpu: bool
    port_expose: bool
    host_mounts: bool
    enforces_limits: frozenset[EnforcedLimit]
    max_concurrency: int | None
    max_lifetime_s: int | None
    max_resources: Resources | None
    typical_start_s: float
    reset_cost_s: float
    template_kinds: frozenset[TemplateKind] = frozenset({"oci"})
    """Template kinds ``create`` boots. A carrier that boots its own built templates instead
    of OCI images declares that kind, and the control plane hands it the template built for
    it from the plan's OCI image (TECH-DESIGN 7.6)."""


@dataclass
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    duration_s: float
    truncated: bool = False


OutputCallback = Callable[[Literal["stdout", "stderr"], bytes], None]

TIMEOUT_EXIT_CODE = 124


class Sandbox(ABC):
    """One live sandbox instance. Safe for concurrent ``exec`` calls."""

    id: str
    spec: SandboxSpec

    @abstractmethod
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
        """Run a command.

        A ``list`` command is never passed through a shell; a ``str`` uses the OS default shell
        (/bin/sh -lc on linux and macos, powershell -NoProfile -Command on windows). On timeout
        the whole process group or job object is killed and the result has ``timed_out=True``
        and ``exit_code=124``. Providers without streaming deliver ``on_output`` once, at the end.
        """

    @abstractmethod
    async def upload(self, local: Path, remote: str) -> None:
        """Upload a file or directory (directories travel as tar or zip), keeping permission bits."""

    @abstractmethod
    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        """Download a file or directory. Callers re-validate the result locally (invariant I11)."""

    @abstractmethod
    async def destroy(self) -> None:
        """Idempotent: destroying a missing instance succeeds."""

    async def snapshot(self, name: str) -> str:
        raise CapabilityNotSupported("snapshot")

    async def set_network(self, policy: NetworkPolicy) -> None:
        raise CapabilityNotSupported("dynamic_network_policy")

    async def gui_endpoint(self) -> str:
        raise CapabilityNotSupported("gui")

    async def expose_port(self, port: int) -> str:
        raise CapabilityNotSupported("port_expose")

    async def describe(self) -> dict[str, Any]:
        """Host, effective resources and the template digest actually observed."""
        return {}


class SandboxProvider(ABC):
    """A sandbox vendor or host. One object per configured provider instance."""

    name: str
    """Adapter name, for example ``novita-sandbox``."""

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        self.instance = instance
        self.config = config
        self.secrets = secrets

    @abstractmethod
    def capabilities(self) -> SandboxCapabilities: ...

    @abstractmethod
    def isolation_profile(self) -> IsolationProfile: ...

    @abstractmethod
    async def preflight(self) -> dict[str, Any]:
        """Credentials, quota headroom, host health, template availability."""

    @abstractmethod
    async def create(self, spec: SandboxSpec) -> Sandbox:
        """Return a sandbox that can already execute commands.

        Raises ``CapabilityNotSupported`` or ``PolicyNotEnforceable`` when ``spec`` is not a subset
        of the declared capabilities. The instance lifetime never exceeds ``spec.max_lifetime_s``.
        """

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        raise CapabilityNotSupported("snapshot")

    async def environment(self) -> ExecutionEnvironment:
        """Host, runtime and guest this instance runs sandboxes on (reproducibility).

        Only stable facts: never memory in use, uptime or load. The default reports
        nothing (every field ``None``); an adapter fills in what its carrier can tell
        and leaves the rest ``None``. A carrier whose engine runs on another machine
        must not report the control-plane machine as its host. Raise a ``SpleefError``
        when the carrier cannot be asked; the control plane records the reason.
        """
        return ExecutionEnvironment()

    @abstractmethod
    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Destroy orphans matching ``labels``; returns how many were destroyed."""
