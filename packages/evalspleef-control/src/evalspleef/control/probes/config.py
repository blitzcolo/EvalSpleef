"""Probe inputs: what is being probed (target) and how (config)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PROBE_IDS", "ProbeConfig", "ProbeTarget"]

PROBE_IDS = tuple(f"P-{number:02d}" for number in range(1, 16))


@dataclass(frozen=True)
class ProbeTarget:
    """One endpoint as the probe key sees it."""

    endpoint_id: str
    model_name: str
    """Public model name registered on the gateway (``ep_<...>``)."""
    declared_context_tokens: int
    declared_max_output_tokens: int
    expected_models: frozenset[str] = frozenset()
    """Accepted values of the response ``model`` field (P-14 identity)."""
    expected_system_fingerprint: str | None = None


@dataclass(frozen=True)
class ProbeConfig:
    request_timeout_s: float = 60.0
    long_request_timeout_s: float = 600.0
    """Timeout for the large-prompt (P-09) and long-output (P-10) requests."""
    connect_timeout_s: float = 10.0
    key_ttl_s: float = 3600.0
    key_budget_usd: float = 25.0
    key_token_cap: int = 3_000_000
    tool_samples: int = 5
    tool_json_min_rate: float = 0.8
    """Minimum share of P-03 samples with a valid tool call for ``tool_calling >= single``."""
    context_floor_tokens: int = 256
    context_resolution_tokens: int = 512
    """P-09 stops bisecting once the bracket is this narrow."""
    needle_max_tokens: int = 1024
    """Answer budget of a P-09 request; reasoning models spend part of it thinking first."""
    max_output_probe_tokens: int | None = None
    """Cap for the P-10 request; the declared maximum output is used when None."""
    max_output_halvings: int = 4
    throughput_concurrency: tuple[int, ...] = (1, 4, 16)
    throughput_requests: int = 20
    throughput_max_tokens: int = 32
    enable_fingerprint: bool = False
    """P-15 is optional and off by default."""
    fingerprint_reference: str | None = None
    include: frozenset[str] | None = None
    """Restrict the run to these probe ids; all enabled probes run when None."""
