"""In-memory implementation of the ``GatewayAdmin`` SPI.

The control plane drives the real gateway through this interface; tests drive this double.
It implements every abstract method of ``GatewayAdmin`` plus the test-side extras the contract
asks for: ``record_usage`` costs a served request from the frozen price of its route,
``authenticate`` validates a bearer secret against revocation and clock-based expiry, and
``spent_usd``, ``tokens_used`` and ``budget_status`` report per-trial accounting.
"""

from __future__ import annotations

import secrets as _secrets
from datetime import UTC, datetime

from evalspleef.spi.gateway import (
    COST_CAP,
    TOKEN_CAP,
    GatewayAdmin,
    PriceSpec,
    TrialKey,
    UsageRecord,
)
from evalspleef.spi.model import UpstreamRoute

from .clock import Clock, SystemClock

VERSION = "in-memory-gateway/0.1"


def _parse_instant(value: str) -> float:
    """Parse an ISO 8601 timestamp into epoch seconds, assuming UTC when naive."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _format_instant(epoch: float) -> str:
    """Format epoch seconds as the ISO 8601 UTC string the SPI uses."""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class TrialKeyRejected(Exception):
    """A bearer secret is unknown, revoked, or past its expiry."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"trial key rejected: {reason}")
        self.reason = reason


class InMemoryGatewayAdmin(GatewayAdmin):
    """Routes, prices, trial keys and usage records held in process memory."""

    def __init__(self, *, clock: Clock | None = None, version: str = VERSION) -> None:
        self._clock: Clock = clock or SystemClock()
        self._version = version
        self._routes: dict[str, UpstreamRoute] = {}
        self._prices: dict[str, PriceSpec] = {}
        self._keys_by_secret: dict[str, TrialKey] = {}
        self._secrets_by_key_id: dict[str, str] = {}
        self._revoked: set[str] = set()
        self._usage: list[UsageRecord] = []
        self._key_counter = 0

    @property
    def routes(self) -> dict[str, UpstreamRoute]:
        """The routes registered so far, for assertions."""
        return self._routes

    async def version(self) -> str:
        return self._version

    async def upsert_route(self, name: str, route: UpstreamRoute) -> None:
        self._routes[name] = route

    async def remove_route(self, name: str) -> None:
        self._routes.pop(name, None)

    async def set_price(self, name: str, price: PriceSpec) -> None:
        self._prices[name] = price

    async def issue_key(
        self,
        *,
        trial_id: str,
        endpoint_id: str,
        max_budget_usd: float,
        max_tokens: int,
        expires_at: str,
    ) -> TrialKey:
        self._key_counter += 1
        key = TrialKey(
            key_id=f"tk_{self._key_counter}",
            secret=f"sk-test-{_secrets.token_hex(16)}",
            trial_id=trial_id,
            endpoint_id=endpoint_id,
            max_budget_usd=max_budget_usd,
            max_tokens=max_tokens,
            expires_at=expires_at,
        )
        self._keys_by_secret[key.secret] = key
        self._secrets_by_key_id[key.key_id] = key.secret
        return key

    async def revoke_key(self, key_id: str) -> None:
        """Idempotent: revoking a missing or already-revoked key succeeds."""
        secret = self._secrets_by_key_id.get(key_id)
        if secret is not None:
            self._revoked.add(secret)

    async def usage(self, trial_id: str) -> list[UsageRecord]:
        return [record for record in self._usage if record.trial_id == trial_id]

    # -- test-side extras --------------------------------------------------------

    def authenticate(self, secret: str) -> TrialKey:
        """Resolve a bearer secret, rejecting revoked, unknown and expired keys."""
        if secret in self._revoked:
            raise TrialKeyRejected("revoked")
        key = self._keys_by_secret.get(secret)
        if key is None:
            raise TrialKeyRejected("unknown")
        if self._clock.now() >= _parse_instant(key.expires_at):
            raise TrialKeyRejected("expired")
        return key

    def is_revoked(self, key_id: str) -> bool:
        secret = self._secrets_by_key_id.get(key_id)
        return secret is not None and secret in self._revoked

    def record_refusal(
        self,
        *,
        trial_id: str,
        round: int,
        endpoint_id: str,
        error_code: str,
        status_code: int = 502,
    ) -> UsageRecord:
        """Append the uncharged usage record of a request the gateway refused (``error_code``)."""
        record = UsageRecord(
            ts=_format_instant(self._clock.now()),
            trial_id=trial_id,
            round=round,
            endpoint_id=endpoint_id,
            upstream_model="",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            reasoning_tokens=0,
            latency_ms=0,
            status_code=status_code,
            cold_start_ms=0,
            cost_usd=0.0,
            error_code=error_code,
        )
        self._usage.append(record)
        return record

    def record_usage(
        self,
        *,
        trial_id: str,
        round: int,
        route_name: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        latency_ms: int = 0,
        status_code: int = 200,
        cold_start_ms: int = 0,
    ) -> UsageRecord:
        """Cost one served request from the route's frozen price and append its usage event."""
        route = self._routes.get(route_name)
        if route is None:
            raise KeyError(f"no route {route_name!r}")
        price = self._prices.get(route_name)
        if price is None:
            raise KeyError(f"no price for route {route_name!r}")
        cost_usd = (
            input_tokens * price.input_per_mtok
            + output_tokens * price.output_per_mtok
            + cached_tokens * price.cached_per_mtok
        ) / 1_000_000
        record = UsageRecord(
            ts=_format_instant(self._clock.now()),
            trial_id=trial_id,
            round=round,
            endpoint_id=route_name,
            upstream_model=route.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            latency_ms=latency_ms,
            status_code=status_code,
            cold_start_ms=cold_start_ms,
            cost_usd=cost_usd,
        )
        self._usage.append(record)
        return record

    def spent_usd(self, trial_id: str) -> float:
        return sum(record.cost_usd for record in self._usage if record.trial_id == trial_id)

    def tokens_used(self, trial_id: str) -> int:
        return sum(
            record.input_tokens + record.output_tokens
            for record in self._usage
            if record.trial_id == trial_id
        )

    def budget_status(self, trial_id: str) -> str | None:
        """``COST_CAP`` or ``TOKEN_CAP`` once a trial reaches a hard limit, else ``None``."""
        key = next(
            (key for key in self._keys_by_secret.values() if key.trial_id == trial_id),
            None,
        )
        if key is None:
            return None
        if self.spent_usd(trial_id) >= key.max_budget_usd:
            return COST_CAP
        if self.tokens_used(trial_id) >= key.max_tokens:
            return TOKEN_CAP
        return None
