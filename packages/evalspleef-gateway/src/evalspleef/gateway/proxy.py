"""Upstream forwarding: one endpoint speaks Chat Completions, with cold-start absorption.

The gateway always forwards as ``POST {api_base}/chat/completions``. When the route has
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
from typing import Any

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
    ) -> OpenedStream:
        """Consume SSE lines until the first chunk carrying ``lock_field`` appears."""
        stream = cls(response, [], None, cold_start_ms)
        stream._line_iter = response.aiter_lines()
        async for line in stream._line_iter:
            stream.buffered_lines.append(line)
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
        for line in self.buffered_lines:
            yield line
        if self._line_iter is None:
            return
        async for line in self._line_iter:
            yield line


def chat_url(route: UpstreamRoute) -> str:
    return f"{route.api_base.rstrip('/')}/chat/completions"


def upstream_headers(state: GatewayState, route: UpstreamRoute) -> dict[str, str]:
    try:
        api_key = state.secrets.resolve(route.api_key_ref)
    except Exception as exc:
        raise UpstreamCredentialError(str(exc)) from exc
    return {"Authorization": f"Bearer {api_key}", **dict(route.extra_headers)}


def apply_default_params(route: UpstreamRoute, body: dict[str, Any]) -> dict[str, Any]:
    """Route defaults with the body winning, except ``max_tokens`` which is a ceiling."""
    merged = dict(body)
    for key, value in route.default_params.items():
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


def projection_for(
    route: UpstreamRoute, price_entry: RouteEntry, body: dict[str, Any]
) -> BudgetProjection:
    """What the admission check reserves: minimum cost plus the requested max_tokens."""
    max_tokens = body.get("max_tokens")
    tokens = max_tokens if isinstance(max_tokens, int) else 0
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
            url=chat_url(route),
            window_s=route.cold_start_window_s,
        )
    if state.clock.now() - started_at + state.cold_start_interval_s > route.cold_start_window_s:
        return False
    await state.clock.sleep(state.cold_start_interval_s)
    return True


async def post_upstream(
    state: GatewayState, entry: RouteEntry, body: dict[str, Any], *, endpoint_id: str
) -> tuple[httpx.Response, int]:
    """Non-streaming forward with cold-start absorption; body is read."""
    route = entry.route
    headers = upstream_headers(state, route)
    started_at = state.clock.now()
    retried = False
    while True:
        try:
            response = await state.client.post(chat_url(route), json=body, headers=headers)
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
    while True:
        request = state.client.build_request("POST", chat_url(route), json=body, headers=headers)
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
        if response.status_code != 200:
            await response.aread()
            cold_start_ms = int((state.clock.now() - started_at) * 1000) if retried else 0
            return OpenedStream(response, [], None, cold_start_ms)
        lock_field = route.provider_lock.response_field if route.provider_lock else None
        cold_start_ms = int((state.clock.now() - started_at) * 1000) if retried else 0
        return await OpenedStream.open(
            response, cold_start_ms=cold_start_ms, lock_field=lock_field, parse=_parse_data_line
        )


def _parse_data_line(line: str) -> dict[str, Any] | None:
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
