"""Upstream forwarding: one endpoint speaks Chat Completions, with cold-start absorption.

The gateway forwards as ``POST {api_base}/chat/completions``, or as ``POST {api_base}/messages``
for a route whose protocol is ``anthropic_messages``. When the route has
``cold_start_window_s > 0`` an HTTP 503 answer or a connect timeout is retried every
``state.cold_start_interval_s`` seconds until the window ends (TECH-DESIGN 6.3): the
client sees a single slow request and the absorbed time is reported as
``cold_start_ms``. The clock is injectable so tests never wait on wall time.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from evalspleef.spi.model import UpstreamRoute

from .state import (
    EVENT_ENDPOINT_COLD_START,
    BudgetProjection,
    GatewayState,
    RouteEntry,
    projected_min_cost,
)


class UpstreamCredentialError(Exception):
    """The route's ``api_key_ref`` could not be resolved."""


@dataclass
class OpenedStream:
    """A streaming upstream response plus the initial SSE lines already consumed.

    Owns a single line iterator so the peeked prefix is never replayed, regardless of
    whether the underlying transport re-iterates its body (in-memory test transports).
    """

    response: httpx.Response
    buffered_lines: list[str]
    first_chunk: dict[str, Any] | None
    cold_start_ms: int
    buffered_at: list[float] = field(default_factory=list)
    """Arrival time of each buffered line on the gateway's timing clock."""
    _line_iter: AsyncIterator[str] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False)

    @classmethod
    async def open(
        cls,
        response: httpx.Response,
        *,
        cold_start_ms: int,
        lock_field: str | None,
        parse: Callable[[str], dict[str, Any] | None],
        now: Callable[[], float],
    ) -> OpenedStream:
        """Consume SSE lines until the first chunk carrying ``lock_field`` appears."""
        stream = cls(response, [], None, cold_start_ms)
        stream._line_iter = response.aiter_lines()
        async for line in stream._line_iter:
            stream.buffered_lines.append(line)
            stream.buffered_at.append(now())
            chunk = parse(line)
            if isinstance(chunk, dict) and (lock_field is None or lock_field in chunk):
                stream.first_chunk = chunk
                break
            if len(stream.buffered_lines) >= 64:
                break
        return stream

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self.response.aclose()

    def iter_lines(self) -> AsyncIterator[str]:
        """Yield the buffered lines first, then the rest of the upstream body."""
        return self._iter_lines()

    async def _iter_lines(self) -> AsyncIterator[str]:
        async for line, _ in self.iter_timed_lines():
            yield line

    async def iter_timed_lines(self) -> AsyncIterator[tuple[str, float | None]]:
        """Like :meth:`iter_lines`, with the arrival time of buffered lines (``None``: now)."""
        for line, at in zip(self.buffered_lines, self.buffered_at, strict=False):
            yield line, at
        for line in self.buffered_lines[len(self.buffered_at) :]:
            yield line, None
        if self._line_iter is None:
            return
        async for line in self._line_iter:
            yield line, None


ANTHROPIC_VERSION = "2026-10-21"

#: Route ``default_params`` keys that steer the gateway and are never sent upstream.
PROMPT_CACHING_KEY = "prompt_caching"
DROP_PARAMS_KEY = "drop_params"
UPSTREAM_TIMEOUT_KEY = "upstream_timeout_s"
"""Read timeout for this route's upstream calls; the gateway default (120 s) when unset."""
DEFAULT_RESERVATION_KEY = "default_reservation_tokens"
"""Output tokens to reserve for a request that carries no output limit; unset reserves 0."""
ROUTE_CONTROL_KEYS = frozenset(
    {PROMPT_CACHING_KEY, DROP_PARAMS_KEY, UPSTREAM_TIMEOUT_KEY, DEFAULT_RESERVATION_KEY}
)


def chat_url(route: UpstreamRoute) -> str:
    return f"{route.api_base.rstrip('/')}/chat/completions"


def upstream_url(route: UpstreamRoute) -> str:
    """The URL a request to this route is posted to, by upstream protocol."""
    if route.protocol == "anthropic_messages":
        return f"{route.api_base.rstrip('/')}/messages"
    return chat_url(route)


@dataclass(frozen=True)
class RouteControls:
    """Gateway-side route switches read from ``default_params``."""

    cache_ttl: Literal["5m", "1h"] | None
    drop_params: tuple[str, ...]


def route_controls(route: UpstreamRoute) -> RouteControls:
    """``prompt_caching``: absent or true -> 5 minutes, ``"1h"`` -> 1 hour, false -> off."""
    caching = route.default_params.get(PROMPT_CACHING_KEY, True)
    ttl: Literal["5m", "1h"] | None = "1h" if caching == "1h" else "5m"
    if caching is False or caching is None:
        ttl = None
    drop = route.default_params.get(DROP_PARAMS_KEY) or ()
    return RouteControls(cache_ttl=ttl, drop_params=tuple(str(name) for name in drop))


def upstream_timeout(route: UpstreamRoute) -> httpx.Timeout | None:
    """The route's own upstream timeout, or ``None`` to use the gateway client's default.

    Long-context prefill and thinking-mode answers can take minutes before the first byte,
    so a self-served route can raise the read timeout above the gateway default.
    """
    value = route.default_params.get(UPSTREAM_TIMEOUT_KEY)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    seconds = float(value)
    return httpx.Timeout(seconds, connect=min(10.0, seconds))


def _timeout_kwargs(route: UpstreamRoute) -> dict[str, Any]:
    timeout = upstream_timeout(route)
    return {} if timeout is None else {"timeout": timeout}


def upstream_headers(state: GatewayState, route: UpstreamRoute) -> dict[str, str]:
    try:
        api_key = state.secrets.resolve(route.api_key_ref)
    except Exception as exc:
        raise UpstreamCredentialError(str(exc)) from exc
    extra = dict(route.extra_headers)
    if route.protocol == "anthropic_messages":
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
        overridden = {name.lower() for name in extra}
        return {**{k: v for k, v in headers.items() if k not in overridden}, **extra}
    return {"Authorization": f"Bearer {api_key}", **extra}


def apply_default_params(route: UpstreamRoute, body: dict[str, Any]) -> dict[str, Any]:
    """Route defaults with the body winning, except ``max_tokens`` which is a ceiling."""
    merged = dict(body)
    for key, value in route.default_params.items():
        if key in ROUTE_CONTROL_KEYS:
            continue
        if key == "max_tokens" and isinstance(value, int):
            requested = merged.get("max_tokens")
            if not isinstance(requested, int) or requested > value:
                merged["max_tokens"] = value
        elif key not in merged:
            merged[key] = value
    if merged.get("stream"):
        stream_options = dict(merged.get("stream_options") or {})
        stream_options["include_usage"] = True
        merged["stream_options"] = stream_options
    return merged


def _requested_output_tokens(body: dict[str, Any]) -> int | None:
    """The body's own output limit: the larger of ``max_completion_tokens`` and ``max_tokens``."""
    requested = [
        value
        for value in (body.get("max_completion_tokens"), body.get("max_tokens"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    return max(requested) if requested else None


def _default_reservation(route: UpstreamRoute) -> int:
    """The route's ``default_reservation_tokens`` control value; unset or invalid means 0."""
    value = route.default_params.get(DEFAULT_RESERVATION_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def projection_for(
    route: UpstreamRoute, price_entry: RouteEntry, body: dict[str, Any]
) -> BudgetProjection:
    """Reserve the body's larger output limit, else the route's default (0 when unset)."""
    tokens = _requested_output_tokens(body)
    if tokens is None:
        tokens = _default_reservation(route)
    return BudgetProjection(cost_usd=projected_min_cost(price_entry.price), tokens=tokens)


def provider_lock_mismatch(route: UpstreamRoute, payload: dict[str, Any]) -> bool:
    """``True`` when the response field naming the real vendor does not match the lock."""
    lock = route.provider_lock
    if lock is None:
        return False
    value = payload.get(lock.response_field)
    if not isinstance(value, str):
        return True
    return re.search(lock.pattern, value) is None


async def _should_retry(
    state: GatewayState,
    route: UpstreamRoute,
    started_at: float,
    event_sent: bool,
    *,
    endpoint_id: str,
) -> bool:
    """Sleep one interval and report ``True`` when another attempt fits the window."""
    if not event_sent:
        await state.emit(
            EVENT_ENDPOINT_COLD_START,
            endpoint_id=endpoint_id,
            url=upstream_url(route),
            window_s=route.cold_start_window_s,
        )
    if state.clock.now() - started_at + state.cold_start_interval_s > route.cold_start_window_s:
        return False
    await state.clock.sleep(state.cold_start_interval_s)
    return True


#: Upstream statuses retried with exponential backoff (PRD appendix B.6: provider rate
#: limits and 5xx are retried up to five times before the trial sees them).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


async def _backoff(state: GatewayState, response: httpx.Response, attempt: int) -> bool:
    """Wait before retry ``attempt`` (0-based) of a retryable status; ``False`` when spent."""
    if response.status_code not in RETRYABLE_STATUSES or attempt >= state.provider_max_retries:
        return False
    delay = state.provider_backoff_s * (2**attempt)
    retry_after = response.headers.get("retry-after", "")
    if retry_after.replace(".", "", 1).isdigit():
        delay = float(retry_after)
    await state.clock.sleep(min(delay, state.provider_backoff_cap_s))
    return True


async def post_upstream(
    state: GatewayState, entry: RouteEntry, body: dict[str, Any], *, endpoint_id: str
) -> tuple[httpx.Response, int]:
    """Non-streaming forward with cold-start absorption and backoff; body is read."""
    route = entry.route
    headers = upstream_headers(state, route)
    started_at = state.clock.now()
    retried = False
    attempts = 0
    while True:
        try:
            response = await state.client.post(
                upstream_url(route), json=body, headers=headers, **_timeout_kwargs(route)
            )
        except httpx.ConnectTimeout:
            if route.cold_start_window_s > 0 and await _should_retry(
                state, route, started_at, retried, endpoint_id=endpoint_id
            ):
                retried = True
                continue
            raise
        if (
            response.status_code == 503
            and route.cold_start_window_s > 0
            and await _should_retry(state, route, started_at, retried, endpoint_id=endpoint_id)
        ):
            retried = True
            continue
        if await _backoff(state, response, attempts):
            attempts += 1
            continue
        cold_start_ms = int((state.clock.now() - started_at) * 1000) if retried else 0
        return response, cold_start_ms


async def open_upstream_stream(
    state: GatewayState, entry: RouteEntry, body: dict[str, Any], *, endpoint_id: str
) -> OpenedStream:
    """Streaming forward with cold-start absorption.

    Reads initial SSE lines until the first chunk carrying the provider-lock field (or
    the first chunk at all when there is no lock) so a mismatch can still be answered
    with a real HTTP status.
    """
    route = entry.route
    headers = upstream_headers(state, route)
    started_at = state.clock.now()
    retried = False
    attempts = 0
    while True:
        request = state.client.build_request(
            "POST", upstream_url(route), json=body, headers=headers, **_timeout_kwargs(route)
        )
        try:
            response = await state.client.send(request, stream=True)
        except httpx.ConnectTimeout:
            if route.cold_start_window_s > 0 and await _should_retry(
                state, route, started_at, retried, endpoint_id=endpoint_id
            ):
                retried = True
                continue
            raise
        if (
            response.status_code == 503
            and route.cold_start_window_s > 0
            and await _should_retry(state, route, started_at, retried, endpoint_id=endpoint_id)
        ):
            retried = True
            await response.aclose()
            continue
        if response.status_code in RETRYABLE_STATUSES and attempts < state.provider_max_retries:
            await response.aread()
            await response.aclose()
            await _backoff(state, response, attempts)
            attempts += 1
            continue
        if response.status_code != 200:
            await response.aread()
            cold_start_ms = int((state.clock.now() - started_at) * 1000) if retried else 0
            return OpenedStream(response, [], None, cold_start_ms)
        lock_field = route.provider_lock.response_field if route.provider_lock else None
        cold_start_ms = int((state.clock.now() - started_at) * 1000) if retried else 0
        return await OpenedStream.open(
            response,
            cold_start_ms=cold_start_ms,
            lock_field=lock_field,
            parse=parse_message_start
            if route.protocol == "anthropic_messages"
            else parse_data_line,
            now=state.perf_now,
        )


def parse_message_start(line: str) -> dict[str, Any] | None:
    """The ``message`` object of an Anthropic ``message_start`` data line, else ``None``.

    It carries the upstream model and id, which is what the provider lock inspects.
    """
    event = parse_data_line(line)
    if event is None or event.get("type") != "message_start":
        return None
    message = event.get("message")
    return message if isinstance(message, dict) else None


def parse_data_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data: "):
        return None
    payload = line[len("data: ") :]
    if payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
