"""Shared gateway state: routes, prices, keys, budgets, usage and events.

Both FastAPI applications (data plane and admin plane) operate on one
:class:`GatewayState`. The state owns the key/usage stores, the upstream HTTP client,
the secret resolver, the in-memory sequenced event log and the per-key accounting
locks.

Budget enforcement (PRD appendix B.5, TECH-DESIGN 6.3): before a request is forwarded
the spent totals are checked under the key's lock against a projected minimum cost and
the requested ``max_tokens``. The projection is *reserved* while the request is in
flight, so concurrent requests see each other's reservations: the totals can exceed the
budget only by the un-projected remainder of requests that were already in flight when
the budget ran out — never more than the in-flight set allows.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets as secrets_mod
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP, PriceSpec, TrialKey, UsageRecord
from evalspleef.spi.model import UpstreamRoute
from evalspleef.spi.secrets import SecretResolver

from .store import KeyStore, UsageStore
from .store import StoredKey as StoredKey

GATEWAY_VERSION = "0.1.0"
RATE_WINDOW_S = 60.0
"""Window of the per-route rpm/tpm limits."""

COLD_START_HEADER = "x-spleef-cold-start-ms"
STOP_REASON_HEADER = "x-spleef-stop-reason"
ROUND_HEADER = "x-spleef-round"
GATEWAY_VERSION_HEADER = "x-spleef-gateway-version"

EVENT_KEY_ISSUED = "key.issued"
EVENT_KEY_REVOKED = "key.revoked"
EVENT_REQUEST_COMPLETED = "request.completed"
EVENT_BUDGET_REJECTED = "budget.rejected"
EVENT_ENDPOINT_COLD_START = "endpoint.cold_start"
EVENT_ENDPOINT_MISMATCH = "endpoint.mismatch"

MTok = 1_000_000.0


class Clock(Protocol):
    """Injectable clock; ``now`` is epoch seconds, ``sleep`` is an interruptible wait."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return datetime.now(tz=UTC).timestamp()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class GatewayEvent:
    """One entry of the sequenced admin event log."""

    seq: int
    ts: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, **self.data}


@dataclass(frozen=True)
class RouteEntry:
    """An upstream route plus its (separately managed) price."""

    route: UpstreamRoute
    price: PriceSpec | None = None


def hash_secret(secret: str) -> str:
    """The only form in which a trial key secret is persisted."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def utc_now_iso(clock: Clock) -> str:
    return datetime.fromtimestamp(clock.now(), tz=UTC).isoformat()


def is_expired(expires_at: str, now: float) -> bool:
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() <= now


def compute_cost(
    price: PriceSpec | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
) -> float:
    """Cost in USD from the frozen per-route price table; zero when no price is set."""
    if price is None:
        return 0.0
    billable_input = max(input_tokens - cached_tokens, 0)
    cost = (
        billable_input * price.input_per_mtok
        + cached_tokens * price.cached_per_mtok
        + output_tokens * price.output_per_mtok
    ) / MTok
    return round(cost, 12)


def projected_min_cost(price: PriceSpec | None) -> float:
    """The minimum a real request can cost: one input and one output token."""
    if price is None:
        return 0.0
    return round((price.input_per_mtok + price.output_per_mtok) / MTok, 12)


@dataclass(frozen=True)
class BudgetProjection:
    """What the admission check reserves for one in-flight request."""

    cost_usd: float
    tokens: int


class GatewayState:
    """Everything both apps share. All methods are safe for concurrent use."""

    def __init__(
        self,
        *,
        key_store: KeyStore,
        usage_store: UsageStore,
        secrets: SecretResolver,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        cold_start_interval_s: float = 15.0,
        upstream_timeout_s: float = 120.0,
        provider_max_retries: int = 5,
        provider_backoff_s: float = 1.0,
        provider_backoff_cap_s: float = 30.0,
    ) -> None:
        self.key_store = key_store
        self.usage_store = usage_store
        self.secrets = secrets
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.cold_start_interval_s = cold_start_interval_s
        self.upstream_timeout_s = upstream_timeout_s
        self.provider_max_retries = provider_max_retries
        self.provider_backoff_s = provider_backoff_s
        self.provider_backoff_cap_s = provider_backoff_cap_s
        self._owns_client = client is None
        timeout = httpx.Timeout(upstream_timeout_s, connect=min(10.0, upstream_timeout_s))
        self.client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._routes: dict[str, RouteEntry] = {}
        self._events: list[GatewayEvent] = []
        self._events_lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_locks_guard = asyncio.Lock()
        self._in_flight: dict[str, BudgetProjection] = {}
        self._rate_windows: dict[str, deque[tuple[float, int]]] = {}
        self._rate_lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self.client.aclose()

    # -- routes and prices -------------------------------------------------------

    def upsert_route(self, name: str, route: UpstreamRoute) -> None:
        existing = self._routes.get(name)
        price = existing.price if existing is not None else None
        self._routes[name] = RouteEntry(route=route, price=price)

    def remove_route(self, name: str) -> bool:
        return self._routes.pop(name, None) is not None

    def set_price(self, name: str, price: PriceSpec) -> None:
        entry = self._routes.get(name)
        if entry is None:
            raise KeyError(f"unknown route: {name!r}")
        self._routes[name] = RouteEntry(route=entry.route, price=price)

    def route_entry(self, name: str) -> RouteEntry | None:
        return self._routes.get(name)

    def public_name_for_endpoint(self, endpoint_id: str) -> str:
        """The model name a trial key may call: the endpoint id itself (``ep_<slug>_<hash>``)."""
        return endpoint_id

    # -- keys --------------------------------------------------------------------

    async def issue_key(
        self,
        *,
        trial_id: str,
        endpoint_id: str,
        max_budget_usd: float,
        max_tokens: int,
        expires_at: str,
    ) -> TrialKey:
        key_id = f"tk_{secrets_mod.token_hex(8)}"
        secret = f"sk-trial-{secrets_mod.token_urlsafe(24)}"
        stored = StoredKey(
            key_id=key_id,
            secret_sha256=hash_secret(secret),
            trial_id=trial_id,
            endpoint_id=endpoint_id,
            max_budget_usd=max_budget_usd,
            max_tokens=max_tokens,
            expires_at=expires_at,
        )
        await self.key_store.insert_key(stored)
        await self._emit(
            EVENT_KEY_ISSUED,
            key_id=key_id,
            trial_id=trial_id,
            endpoint_id=endpoint_id,
            max_budget_usd=max_budget_usd,
            max_tokens=max_tokens,
            expires_at=expires_at,
        )
        return TrialKey(
            key_id=key_id,
            secret=secret,
            trial_id=trial_id,
            endpoint_id=endpoint_id,
            max_budget_usd=max_budget_usd,
            max_tokens=max_tokens,
            expires_at=expires_at,
        )

    async def authenticate(self, secret: str) -> StoredKey | None:
        """Resolve a plaintext secret to a live key, or ``None`` (unknown/revoked/expired)."""
        stored = await self.key_store.key_by_secret_hash(hash_secret(secret))
        if stored is None or stored.revoked:
            return None
        if is_expired(stored.expires_at, self.clock.now()):
            return None
        return stored

    async def revoke_key(self, key_id: str) -> bool:
        """Idempotent revoke; returns ``True`` when the key transitioned to revoked."""
        stored = await self.key_store.key_by_id(key_id)
        if stored is None or stored.revoked:
            return False
        await self.key_store.set_revoked(key_id)
        await self._emit(EVENT_KEY_REVOKED, key_id=key_id, trial_id=stored.trial_id)
        return True

    async def _lock_for(self, key_id: str) -> asyncio.Lock:
        async with self._key_locks_guard:
            lock = self._key_locks.get(key_id)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key_id] = lock
            return lock

    async def admit_rate(self, name: str, route: UpstreamRoute, tokens: int) -> float | None:
        """Admit one request against the route's rpm/tpm over the last 60 s.

        Returns None when admitted (and counts it), else the seconds until the oldest counted
        request leaves the window.
        """
        if route.rpm is None and route.tpm is None:
            return None
        async with self._rate_lock:
            now = self.clock.now()
            window = self._rate_windows.setdefault(name, deque())
            while window and window[0][0] <= now - RATE_WINDOW_S:
                window.popleft()
            over_rpm = route.rpm is not None and len(window) >= route.rpm
            used = sum(counted for _, counted in window)
            over_tpm = route.tpm is not None and window and used + tokens > route.tpm
            if over_rpm or over_tpm:
                return max(0.0, window[0][0] + RATE_WINDOW_S - now)
            window.append((now, tokens))
            return None

    async def check_budget(self, key: StoredKey, projection: BudgetProjection) -> str | None:
        """Admission check under the key's lock; reserves the projection while in flight.

        Returns ``COST_CAP`` or ``TOKEN_CAP`` when the request must be rejected, else
        ``None`` and the projection is registered as in-flight.
        """
        lock = await self._lock_for(key.key_id)
        async with lock:
            fresh = await self.key_store.key_by_id(key.key_id)
            if fresh is None or fresh.revoked or is_expired(fresh.expires_at, self.clock.now()):
                return COST_CAP
            reserved = self._in_flight.get(key.key_id, BudgetProjection(0.0, 0))
            spent_cost = fresh.spent_cost_usd + reserved.cost_usd
            spent_tokens = fresh.spent_tokens + reserved.tokens
            if spent_cost + projection.cost_usd > fresh.max_budget_usd:
                return COST_CAP
            if spent_tokens + projection.tokens > fresh.max_tokens:
                return TOKEN_CAP
            self._in_flight[key.key_id] = BudgetProjection(
                cost_usd=reserved.cost_usd + projection.cost_usd,
                tokens=reserved.tokens + projection.tokens,
            )
            return None

    async def release_budget(self, key_id: str, projection: BudgetProjection) -> None:
        """Drop the in-flight reservation (called in a ``finally`` after forwarding)."""
        lock = await self._lock_for(key_id)
        async with lock:
            reserved = self._in_flight.get(key_id)
            if reserved is not None:
                remaining = BudgetProjection(
                    cost_usd=reserved.cost_usd - projection.cost_usd,
                    tokens=reserved.tokens - projection.tokens,
                )
                if remaining.cost_usd <= 0 and remaining.tokens <= 0:
                    self._in_flight.pop(key_id, None)
                else:
                    self._in_flight[key_id] = remaining

    async def charge(self, key_id: str, *, cost_usd: float, tokens: int) -> StoredKey | None:
        """Add real usage to the key's spent totals; ``None`` when the key is gone."""
        lock = await self._lock_for(key_id)
        async with lock:
            try:
                return await self.key_store.add_spend(key_id, cost_usd=cost_usd, tokens=tokens)
            except KeyError:
                return None

    # -- usage and events ----------------------------------------------------------

    async def append_usage(self, record: UsageRecord) -> None:
        await self.usage_store.append(record)

    async def usage_for(self, trial_id: str) -> list[UsageRecord]:
        return await self.usage_store.for_trial(trial_id)

    async def _emit(self, event_type: str, **data: Any) -> GatewayEvent:
        async with self._events_lock:
            event = GatewayEvent(
                seq=len(self._events) + 1,
                ts=utc_now_iso(self.clock),
                type=event_type,
                data=data,
            )
            self._events.append(event)
            return event

    async def emit(self, event_type: str, **data: Any) -> GatewayEvent:
        """Public event emission for the data plane (cold starts, mismatches, ...)."""
        return await self._emit(event_type, **data)

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events if event.seq > after]
