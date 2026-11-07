"""Conversion of ``runtime.requirements`` to SPI ``RuntimeRequirements``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evalspleef.spi.caps import (
    Arch,
    ExecutionMode,
    Isolation,
    ModelRequirements,
    NetMode,
    OSFamily,
    RuntimeRequirements,
)

from .errors import TaskPackError

__all__ = ["requirements_from_dict"]

_ISOLATION_NAMES = {1: Isolation.L1, 2: Isolation.L2, 3: Isolation.L3, 4: Isolation.L4}


def _isolation(value: Any, context: str) -> Isolation:
    if isinstance(value, int) and value in _ISOLATION_NAMES:
        return _ISOLATION_NAMES[value]
    if isinstance(value, str) and value.upper().startswith("L"):
        try:
            return _ISOLATION_NAMES[int(value[1:])]
        except (ValueError, KeyError):
            pass
    raise TaskPackError(f"{context}: min_isolation must be L1..L4, got {value!r}")


def requirements_from_dict(
    data: Mapping[str, Any],
    *,
    execution_mode: ExecutionMode = "remote-tools",
    model_requirements: Mapping[str, Any] | None = None,
    context: str = "runtime.requirements",
) -> RuntimeRequirements:
    """Build an SPI requirements object from a ``runtime.requirements`` block.

    ``model_requirements`` is the sibling ``runtime.model_requirements`` block
    (FR-TSK-01); ``allow_hosts`` lives inside ``runtime.requirements`` itself.
    Enum spellings follow PRD appendix A/D verbatim; unknown values raise
    :class:`TaskPackError` naming the offending field.
    """
    os_raw = data.get("os", "linux")
    try:
        os_family = OSFamily(os_raw)
    except ValueError:
        raise TaskPackError(f"{context}.os: unknown OS family {os_raw!r}") from None

    arch_raw = data.get("arch", ["x86_64"])
    if not isinstance(arch_raw, list | tuple):
        raise TaskPackError(f"{context}.arch: must be a list, got {arch_raw!r}")
    try:
        arches = frozenset(Arch(str(item)) for item in arch_raw)
    except ValueError as exc:
        raise TaskPackError(f"{context}.arch: unknown architecture ({exc})") from None

    network_raw = data.get("network", "off")
    try:
        network = NetMode(network_raw)
    except ValueError:
        raise TaskPackError(f"{context}.network: unknown network mode {network_raw!r}") from None

    allow_hosts_raw = data.get("allow_hosts", ())
    if not isinstance(allow_hosts_raw, list | tuple):
        raise TaskPackError(f"{context}.allow_hosts: must be a list")

    model_raw: Mapping[str, Any] = model_requirements if model_requirements is not None else {}
    tool_calling = model_raw.get("tool_calling", "none")
    if tool_calling not in ("required", "optional", "none"):
        raise TaskPackError(
            f"{context}.model_requirements.tool_calling: unknown value {tool_calling!r}"
        )

    return RuntimeRequirements(
        os=os_family,
        arch=arches,
        min_isolation=_isolation(data.get("min_isolation", "L1"), context),
        network=network,
        allow_hosts=tuple(str(host) for host in allow_hosts_raw),
        gpu=bool(data.get("gpu", False)),
        gui=bool(data.get("gui", False)),
        cpus=float(data.get("cpus", 2)),
        memory_mb=int(data.get("memory_mb", 4096)),
        disk_mb=int(data.get("disk_mb", 10240)),
        max_lifetime_s=int(data.get("max_lifetime_s", 3600)),
        execution_mode=execution_mode,
        model=ModelRequirements(
            tool_calling=tool_calling,
            min_context_tokens=int(model_raw.get("min_context_tokens", 0)),
        ),
    )
