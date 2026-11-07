"""Capability vocabulary shared by providers, the scheduler and task packs.

Spellings follow PRD appendix A and D; they are part of the on-disk contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Literal


class OSFamily(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    HARMONY = "harmony"


class Arch(StrEnum):
    X86_64 = "x86_64"
    ARM64 = "arm64"


class Isolation(IntEnum):
    """Isolation strength L1..L4 (PRD appendix D.1). L0 is forbidden and not modelled."""

    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


class NetMode(StrEnum):
    """Mirrors task.yaml network.mode verbatim (PRD appendix D.3)."""

    OFF = "off"
    DOCS_WHITELIST = "docs_whitelist"
    PACKAGE_MIRROR = "package_mirror"
    FULL = "full"


AllowKind = Literal["hostname", "wildcard", "ipv4", "ipv6", "cidr"]
ExecutionMode = Literal["remote-tools", "in-sandbox"]
ToolCallingRequirement = Literal["required", "optional", "none"]


@dataclass(frozen=True)
class ModelRequirements:
    """What a task needs from the model endpoint (task.yaml runtime.model_requirements)."""

    tool_calling: ToolCallingRequirement = "none"
    min_context_tokens: int = 0


@dataclass(frozen=True)
class RuntimeRequirements:
    """What a task needs from a sandbox (task.yaml runtime.requirements, PRD FR-TSK-01)."""

    os: OSFamily
    arch: frozenset[Arch]
    min_isolation: Isolation
    network: NetMode
    allow_hosts: tuple[str, ...] = ()
    gpu: bool = False
    gui: bool = False
    cpus: float = 2
    memory_mb: int = 4096
    disk_mb: int = 10240
    max_lifetime_s: int = 3600
    execution_mode: ExecutionMode = "remote-tools"
    model: ModelRequirements = field(default_factory=ModelRequirements)
