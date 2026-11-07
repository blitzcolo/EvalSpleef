"""Inference-plane probes and wake-up routines for vpc-vllm endpoints.

Sends minimal requests to verify endpoint responsiveness and maps HTTP responses
to typed platform outcomes and errors.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from evalspleef.spi.errors import EndpointUnavailable, SpleefError

from .clock import Clock
from .errors import (
    AccessForbidden,
    CredentialRejected,
    DeploymentNotFound,
    RequestRejected,
)

Outcome = Literal[
    "ok",
    "not_ready",
    "timeout",
    "unauthorized",
    "forbidden",
    "not_found",
    "rejected",
    "unavailable",
]

DEFAULT_NOT_READY_MARKERS: tuple[str, ...] = (
    "not ready",
    "not_ready",
    "model is loading",
    "cold start",
    "starting up",
    "scaling up",
    "warming up",
)
DEFAULT_WAKE_POLL_S = 15.0
DEFAULT_PROBE_TIMEOUT_S = 90.0
_NOT_READY_STATUSES = frozenset({502, 503, 504})
_CONNECT_TIMEOUT_S = 10.0
_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class ProbeResult:
    outcome: Outcome
    http_status: int | None
    elapsed_s: float
    model: str | None
    detail: str


def chat_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _snippet(response: httpx.Response) -> str:
    return response.text[:_SNIPPET_CHARS].replace("\n", " ")


def classify(
    response: httpx.Response,
    not_ready_markers: tuple[str, ...] = DEFAULT_NOT_READY_MARKERS,
) -> tuple[Outcome, str | None, str]:
    """Map one HTTP response to ``(outcome, model, detail)``."""
    status = response.status_code
    if status == 200:
        try:
            body = response.json()
        except json.JSONDecodeError:
            return "unavailable", None, "HTTP 200 with non-JSON body"
        model = body.get("model") if isinstance(body, dict) else None
        if not isinstance(model, str):
            return "unavailable", None, "HTTP 200 without string model field"
        return "ok", model, "HTTP 200"
    if status == 401:
        return "unauthorized", None, "HTTP 401"
    if status == 403:
        return "forbidden", None, "HTTP 403"
    if status == 404:
        return "not_found", None, "HTTP 404"
    if status in _NOT_READY_STATUSES:
        text = response.text.lower()
        if any(marker in text for marker in not_ready_markers):
            return "not_ready", None, f"HTTP {status} not ready: {_snippet(response)}"
    if status in (400, 422):
        return "rejected", None, f"HTTP {status}: {_snippet(response)}"
    return "unavailable", None, f"HTTP {status}: {_snippet(response)}"


async def probe(
    *,
    api_base: str,
    api_key: str,
    model: str,
    clock: Clock,
    timeout_s: float,
    not_ready_markers: tuple[str, ...] = DEFAULT_NOT_READY_MARKERS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeResult:
    """Send one ``max_tokens=1`` chat request and classify the outcome."""
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, _CONNECT_TIMEOUT_S))
    started = clock.now()
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
            response = await client.post(chat_url(api_base), json=payload, headers=headers)
    except httpx.ReadTimeout:
        return ProbeResult("timeout", None, clock.now() - started, None, "read timeout")
    except httpx.TransportError as exc:
        return ProbeResult(
            "unavailable", None, clock.now() - started, None, f"{type(exc).__name__}: {exc}"
        )
    outcome, answered_model, detail = classify(response, not_ready_markers)
    return ProbeResult(outcome, response.status_code, clock.now() - started, answered_model, detail)


async def wake_loop(
    probe_once: Callable[[float], Awaitable[ProbeResult]],
    *,
    clock: Clock,
    timeout_s: float,
    poll_s: float,
    probe_cap_s: float,
) -> tuple[ProbeResult, float]:
    """Probe until endpoint is ready, an error rules out waiting, or deadline is reached."""
    started = clock.now()
    deadline = started + timeout_s
    while True:
        remaining = deadline - clock.now()
        result = await probe_once(max(1.0, min(remaining, probe_cap_s)))
        if result.outcome not in ("not_ready", "timeout"):
            return result, clock.now() - started
        if clock.now() + poll_s >= deadline:
            return result, clock.now() - started
        await clock.sleep(poll_s)


def raise_for_probe(result: ProbeResult, context: str) -> None:
    """Raise the typed error corresponding to a failed probe result."""
    reason = f"http_{result.http_status}" if result.http_status else result.outcome
    detail = {"reason": reason, "http_status": result.http_status}
    error: SpleefError
    match result.outcome:
        case "ok":
            return
        case "unauthorized":
            error = CredentialRejected(f"{context}: HTTP 401 unauthorized", detail=detail)
        case "forbidden":
            error = AccessForbidden(f"{context}: HTTP 403 forbidden", detail=detail)
        case "not_found":
            error = DeploymentNotFound(f"{context}: HTTP 404 not found", detail=detail)
        case "rejected":
            error = RequestRejected(f"{context}: {result.detail}", detail=detail)
        case "not_ready" | "timeout":
            error = EndpointUnavailable(f"{context}: endpoint cold starting", detail=detail)
        case "unavailable":
            error = EndpointUnavailable(f"{context}: {result.detail}", detail=detail)
    raise error
