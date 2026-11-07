"""Capability matching: ``requirements ⊆ capabilities`` (TECH-DESIGN section 7.2 and 6.4)."""

from __future__ import annotations

from .caps import ModelRequirements, NetMode, RuntimeRequirements
from .probe import ModelCapabilities
from .sandbox import SandboxCapabilities


def match_requirements(
    req: RuntimeRequirements, cap: SandboxCapabilities, *, egress_proxy_available: bool
) -> list[str]:
    """Reasons a sandbox cannot host the task; an empty list means it matches."""
    reasons: list[str] = []
    if req.os not in cap.os_families:
        reasons.append(f"os:{req.os}")
    if not (req.arch & cap.arches):
        reasons.append("arch")
    if cap.isolation < req.min_isolation:
        reasons.append(f"isolation:{cap.isolation.name}<{req.min_isolation.name}")
    if req.network not in cap.net_modes:
        reasons.append(f"net:{req.network}")
    if (
        req.network in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR)
        and "hostname" not in cap.allowlist_kinds
        and not egress_proxy_available
    ):
        reasons.append("net:hostname-allowlist")
    if req.gpu and not cap.gpu:
        reasons.append("gpu")
    if req.gui and cap.gui == "none":
        reasons.append("gui")
    if cap.max_lifetime_s is not None and req.max_lifetime_s > cap.max_lifetime_s:
        reasons.append("lifetime")
    limits = cap.max_resources
    if limits is not None and (
        req.cpus > limits.cpus or req.memory_mb > limits.memory_mb or req.disk_mb > limits.disk_mb
    ):
        reasons.append("resources")
    return reasons


def admit_model(req: ModelRequirements, caps: ModelCapabilities) -> list[str]:
    """Reasons an endpoint cannot run the task, as ``capability:<field>`` skip reasons."""
    reasons: list[str] = []
    if req.tool_calling == "required" and caps.tool_calling == "none":
        reasons.append("capability:tool_calling")
    if caps.context_tokens_effective < req.min_context_tokens:
        reasons.append("capability:context_tokens_effective")
    return reasons
