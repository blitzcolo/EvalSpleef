"""Capability probe result types (TECH-DESIGN section 6.4). The probe suite itself lives in control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

ToolCallingLevel = Literal["none", "single", "parallel"]


@dataclass(frozen=True)
class ModelCapabilities:
    chat: bool = False
    streaming: bool = False
    usage_in_stream: bool = False
    tool_calling: ToolCallingLevel = "none"
    tool_call_json_valid_rate: float = 0.0
    multi_turn_tools: bool = False
    tool_choice_modes: frozenset[str] = frozenset()
    json_schema: bool = False
    reasoning_field: str | None = None
    reasoning_toggle: bool = False
    context_tokens_effective: int = 0
    max_output_tokens_effective: int = 0
    seed_deterministic: bool = False
    stop_ok: bool = False
    protocols_ok: frozenset[str] = frozenset()
    identity_ok: bool = False
    throughput: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    """Keyed by concurrency ("1", "4", "16"); values hold ttft_p50, ttft_p95, tps_p50, tps_p95."""
    fingerprint: str | None = None


@dataclass(frozen=True)
class ProbeCheck:
    probe_id: str
    """P-01 .. P-15."""
    passed: bool
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeReport:
    """Immutable; a batch freeze references ``report_id``."""

    report_id: str
    endpoint_id: str
    gateway_version: str
    created_at: str
    checks: tuple[ProbeCheck, ...]
    capabilities: ModelCapabilities
