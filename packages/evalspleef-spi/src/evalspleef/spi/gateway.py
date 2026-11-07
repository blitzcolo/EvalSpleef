"""Gateway administration interface used by the control plane (TECH-DESIGN section 6.3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from .model import UpstreamRoute

COST_CAP = "cost_cap"
TOKEN_CAP = "token_cap"
"""HTTP 429 error.code values returned when a trial key exceeds its budget."""

COLD_START_HEADER = "x-spleef-cold-start-ms"


@dataclass(frozen=True)
class PriceSpec:
    """Frozen per-route price table entry; USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cached_per_mtok: float = 0.0
    basis: Literal["api", "ref-price", "assumed"] = "api"
    table_version: str = ""


@dataclass(frozen=True)
class TrialKey:
    key_id: str
    secret: str
    """The only credential allowed inside a sandbox. Never persisted by the control plane after hand-off."""
    trial_id: str
    endpoint_id: str
    max_budget_usd: float
    max_tokens: int
    expires_at: str
    """ISO 8601, UTC."""


@dataclass(frozen=True)
class UsageRecord:
    ts: str
    trial_id: str
    round: int
    endpoint_id: str
    upstream_model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    latency_ms: int
    status_code: int
    cold_start_ms: int
    cost_usd: float


class GatewayAdmin(ABC):
    """Management surface of the gateway. Inference traffic uses plain HTTP and never this class."""

    @abstractmethod
    async def version(self) -> str: ...

    @abstractmethod
    async def upsert_route(self, name: str, route: UpstreamRoute) -> None:
        """Expose ``route`` under the public model name ``name`` (``ep_<endpoint_id>``)."""

    @abstractmethod
    async def remove_route(self, name: str) -> None: ...

    @abstractmethod
    async def set_price(self, name: str, price: PriceSpec) -> None:
        """Set the frozen price used to cost requests on route ``name``."""

    @abstractmethod
    async def issue_key(
        self,
        *,
        trial_id: str,
        endpoint_id: str,
        max_budget_usd: float,
        max_tokens: int,
        expires_at: str,
    ) -> TrialKey:
        """Bind a short-lived key to one trial and one endpoint, with a hard budget."""

    @abstractmethod
    async def revoke_key(self, key_id: str) -> None:
        """Idempotent. Called when a trial reaches a terminal state."""

    @abstractmethod
    async def usage(self, trial_id: str) -> list[UsageRecord]: ...
