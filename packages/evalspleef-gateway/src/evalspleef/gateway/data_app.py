"""Public data plane: trial-key auth, budget enforcement and protocol endpoints.

Exposes OpenAI Chat Completions, OpenAI Responses and Anthropic Messages on the public
route names ``ep_<endpoint_id>`` while every upstream endpoint speaks Chat Completions
(TECH-DESIGN 6.3, FR-MOD-07). Every response carries ``x-spleef-gateway-version``; usage
is recorded per request and charged against the trial key's budget.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from evalspleef.spi.gateway import COST_HEADER, UsageRecord

from .protocols import (
    ChatToAnthropicStream,
    ChatToResponsesStream,
    anthropic_error_envelope,
    anthropic_request_to_chat,
    chat_response_to_anthropic,
    chat_response_to_responses,
    openai_error_envelope,
    responses_request_to_chat,
)
from .proxy import (
    OpenedStream,
    UpstreamCredentialError,
    apply_default_params,
    open_upstream_stream,
    post_upstream,
    projection_for,
    provider_lock_mismatch,
)
from .state import (
    COLD_START_HEADER,
    EVENT_BUDGET_REJECTED,
    EVENT_ENDPOINT_MISMATCH,
    EVENT_REQUEST_COMPLETED,
    GATEWAY_VERSION,
    GATEWAY_VERSION_HEADER,
    ROUND_HEADER,
    STOP_REASON_HEADER,
    BudgetProjection,
    GatewayState,
    RouteEntry,
    StoredKey,
    compute_cost,
    utc_now_iso,
)

Flavor = Literal["openai", "anthropic"]


def create_data_app(state: GatewayState) -> FastAPI:
    """Build the public data-plane application bound to ``state``."""
    app = FastAPI(title="evalspleef-gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _gateway_version(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers[GATEWAY_VERSION_HEADER] = GATEWAY_VERSION
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": GATEWAY_VERSION}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        key = await _authenticate(state, request, flavor="openai", anthropic=False)
        if isinstance(key, Response):
            return key
        name = state.public_name_for_endpoint(key.endpoint_id)
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": name, "object": "model", "created": 0, "owned_by": "evalspleef"}],
            }
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _handle(state, request, flavor="openai")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await _handle(state, request, flavor="openai", responses=True)

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await _handle(state, request, flavor="anthropic")

    return app


# -- authentication and admission -----------------------------------------------------


def _error_response(
    flavor: Flavor,
    status: int,
    code: str | None,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = (
        anthropic_error_envelope(status, code, message)
        if flavor == "anthropic"
        else openai_error_envelope(status, code, message)
    )
    return JSONResponse(body, status_code=status, headers=headers)


def _extract_secret(request: Request, *, anthropic: bool) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None and authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]
    if anthropic:
        return request.headers.get("x-api-key")
    return None


async def _authenticate(
    state: GatewayState, request: Request, *, flavor: Flavor, anthropic: bool
) -> StoredKey | Response:
    secret = _extract_secret(request, anthropic=anthropic)
    if secret is None:
        return _error_response(flavor, 401, "authentication_error", "missing trial key")
    key = await state.authenticate(secret)
    if key is None:
        return _error_response(
            flavor, 401, "authentication_error", "unknown, revoked or expired trial key"
        )
    return key


def _resolve_route(
    state: GatewayState, key: StoredKey, model: Any, *, flavor: Flavor
) -> tuple[str, RouteEntry] | Response:
    if not isinstance(model, str) or not model:
        return _error_response(flavor, 400, "invalid_request", "a model is required")
    entry = state.route_entry(model)
    if entry is None:
        return _error_response(flavor, 404, "model_not_found", f"unknown model: {model!r}")
    if model != state.public_name_for_endpoint(key.endpoint_id):
        return _error_response(
            flavor, 403, "forbidden", "this trial key is bound to a different endpoint"
        )
    return model, entry


async def _admit(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    body: dict[str, Any],
    *,
    model: str,
    round_: int,
    flavor: Flavor,
) -> BudgetProjection | Response:
    """Rate and budget admission: reserve the projection, or answer 429 with the reason."""
    projection = projection_for(entry.route, entry, body)
    wait_s = await state.admit_rate(model, entry.route, projection.tokens)
    if wait_s is not None:
        await _record_usage(
            state,
            key=key,
            entry=entry,
            model=model,
            round_=round_,
            usage=None,
            latency_ms=0,
            status_code=429,
            cold_start_ms=0,
            charge=False,
        )
        return _error_response(
            flavor,
            429,
            "rate_limited",
            f"route {model} is over its rpm/tpm limit; retry in {wait_s:.0f} s",
            {"retry-after": str(max(1, math.ceil(wait_s)))},
        )
    violation = await state.check_budget(key, projection)
    if violation is None:
        return projection
    headers = {STOP_REASON_HEADER: violation} if flavor == "anthropic" else None
    await state.emit(
        EVENT_BUDGET_REJECTED,
        trial_id=key.trial_id,
        endpoint_id=key.endpoint_id,
        model=model,
        round=round_,
        code=violation,
        projected_cost_usd=projection.cost_usd,
        projected_tokens=projection.tokens,
    )
    await _record_usage(
        state,
        key=key,
        entry=entry,
        model=model,
        round_=round_,
        usage=None,
        latency_ms=0,
        status_code=429,
        cold_start_ms=0,
        charge=False,
    )
    return _error_response(
        flavor, 429, violation, f"trial key budget exhausted ({violation})", headers
    )


def _round_of(request: Request) -> int:
    raw = request.headers.get(ROUND_HEADER)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _int_of(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_counts(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {"input": 0, "output": 0, "cached": 0, "reasoning": 0}
    input_tokens = _int_of(usage.get("prompt_tokens")) or _int_of(usage.get("input_tokens"))
    output_tokens = _int_of(usage.get("completion_tokens")) or _int_of(usage.get("output_tokens"))
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = _int_of(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else 0
    reasoning = (
        _int_of(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, dict)
        else 0
    )
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cached": cached,
        "reasoning": reasoning,
    }


async def _record_usage(
    state: GatewayState,
    *,
    key: StoredKey,
    entry: RouteEntry,
    model: str,
    round_: int,
    usage: dict[str, Any] | None,
    latency_ms: int,
    status_code: int,
    cold_start_ms: int,
    charge: bool,
    upstream_model: str = "",
) -> float:
    """Append the usage record, charge the key on success and emit request.completed.

    Returns the USD charged, which non-streaming responses report in ``COST_HEADER``.
    """
    counts = _usage_counts(usage)
    cost = (
        compute_cost(
            entry.price,
            input_tokens=counts["input"],
            output_tokens=counts["output"],
            cached_tokens=counts["cached"],
        )
        if charge
        else 0.0
    )
    if charge:
        await state.charge(key.key_id, cost_usd=cost, tokens=counts["input"] + counts["output"])
    record = UsageRecord(
        ts=utc_now_iso(state.clock),
        trial_id=key.trial_id,
        round=round_,
        endpoint_id=key.endpoint_id,
        upstream_model=upstream_model,
        input_tokens=counts["input"],
        output_tokens=counts["output"],
        cached_tokens=counts["cached"],
        reasoning_tokens=counts["reasoning"],
        latency_ms=latency_ms,
        status_code=status_code,
        cold_start_ms=cold_start_ms,
        cost_usd=cost,
    )
    await state.append_usage(record)
    await state.emit(
        EVENT_REQUEST_COMPLETED,
        trial_id=key.trial_id,
        endpoint_id=key.endpoint_id,
        model=model,
        upstream_model=upstream_model,
        input_tokens=counts["input"],
        output_tokens=counts["output"],
        cost_usd=cost,
        status_code=status_code,
        latency_ms=latency_ms,
        cold_start_ms=cold_start_ms,
    )
    return cost


# -- request handling -----------------------------------------------------------------


async def _handle(
    state: GatewayState, request: Request, *, flavor: Flavor, responses: bool = False
) -> Response:
    anthropic = flavor == "anthropic"
    key = await _authenticate(state, request, flavor=flavor, anthropic=anthropic)
    if isinstance(key, Response):
        return key
    try:
        raw_body = await request.json()
    except json.JSONDecodeError:
        return _error_response(flavor, 400, "invalid_request", "request body is not valid JSON")
    if not isinstance(raw_body, dict):
        return _error_response(flavor, 400, "invalid_request", "request body must be an object")
    if responses:
        try:
            body = responses_request_to_chat(raw_body)
        except ValueError as exc:
            return _error_response(flavor, 400, "invalid_request", str(exc))
    elif anthropic:
        try:
            body = anthropic_request_to_chat(raw_body)
        except ValueError as exc:
            return _error_response(flavor, 400, "invalid_request", str(exc))
    else:
        body = dict(raw_body)

    resolved = _resolve_route(state, key, body.get("model"), flavor=flavor)
    if isinstance(resolved, Response):
        return resolved
    model, entry = resolved
    if entry.route.protocol != "chat_completions":
        return _error_response(
            flavor,
            502,
            "unsupported_upstream_protocol",
            f"route {model!r} declares upstream protocol {entry.route.protocol!r};"
            " only chat_completions upstreams can be forwarded",
        )

    round_ = _round_of(request)
    admitted = await _admit(state, key, entry, body, model=model, round_=round_, flavor=flavor)
    if isinstance(admitted, Response):
        return admitted
    try:
        return await _forward(
            state,
            key,
            entry,
            body,
            model=model,
            round_=round_,
            flavor=flavor,
            responses=responses,
        )
    finally:
        await state.release_budget(key.key_id, admitted)


def _latency_ms(state: GatewayState, started_at: float) -> int:
    return int((state.clock.now() - started_at) * 1000)


async def _transport_failure(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    model: str,
    round_: int,
    started_at: float,
    flavor: Flavor,
    exc: httpx.TransportError,
) -> Response:
    """Answer an upstream that timed out or dropped the connection: 504 or 502, uncharged."""
    timed_out = isinstance(exc, httpx.TimeoutException)
    status = 504 if timed_out else 502
    await _record_usage(
        state,
        key=key,
        entry=entry,
        model=model,
        round_=round_,
        usage=None,
        latency_ms=_latency_ms(state, started_at),
        status_code=status,
        cold_start_ms=0,
        charge=False,
    )
    if timed_out:
        return _error_response(flavor, 504, "upstream_timeout", f"upstream timed out: {exc!r}")
    return _error_response(flavor, 502, "upstream_error", f"upstream connection failed: {exc!r}")


def _upstream_error_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    error = payload.get("error")
    if not isinstance(error, dict):
        return "upstream error", None
    message = error.get("message")
    code = error.get("code")
    return (
        message if isinstance(message, str) else "upstream error",
        code if isinstance(code, str) else None,
    )


async def _forward(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    body: dict[str, Any],
    *,
    model: str,
    round_: int,
    flavor: Flavor,
    responses: bool,
) -> Response:
    upstream_body = apply_default_params(entry.route, {**body, "model": entry.route.model})
    started_at = state.clock.now()
    if upstream_body.get("stream"):
        return await _forward_stream(
            state,
            key,
            entry,
            upstream_body,
            model=model,
            round_=round_,
            started_at=started_at,
            flavor=flavor,
            responses=responses,
        )
    try:
        upstream, cold_start_ms = await post_upstream(
            state, entry, upstream_body, endpoint_id=key.endpoint_id
        )
    except UpstreamCredentialError as exc:
        return _error_response(flavor, 502, "upstream_error", f"upstream credential: {exc}")
    except httpx.TransportError as exc:
        return await _transport_failure(state, key, entry, model, round_, started_at, flavor, exc)

    latency_ms = _latency_ms(state, started_at)
    try:
        payload: dict[str, Any] = upstream.json()
    except ValueError:
        payload = {}

    if upstream.status_code == 200 and provider_lock_mismatch(entry.route, payload):
        await _emit_mismatch(state, key, entry, model, payload.get("model"))
        await _record_usage(
            state,
            key=key,
            entry=entry,
            model=model,
            round_=round_,
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            latency_ms=latency_ms,
            status_code=502,
            cold_start_ms=cold_start_ms,
            charge=False,
        )
        return _error_response(
            flavor, 502, "endpoint_mismatch", "upstream response violates the provider lock"
        )
    if upstream.status_code != 200:
        await _record_usage(
            state,
            key=key,
            entry=entry,
            model=model,
            round_=round_,
            usage=None,
            latency_ms=latency_ms,
            status_code=upstream.status_code,
            cold_start_ms=cold_start_ms,
            charge=False,
        )
        message, code = _upstream_error_payload(payload)
        return _error_response(flavor, upstream.status_code, code, message)

    cost = await _record_usage(
        state,
        key=key,
        entry=entry,
        model=model,
        round_=round_,
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        latency_ms=latency_ms,
        status_code=200,
        cold_start_ms=cold_start_ms,
        charge=True,
        upstream_model=_upstream_model(payload),
    )
    if responses:
        result: dict[str, Any] = chat_response_to_responses(payload, model=model)
    elif flavor == "anthropic":
        result = chat_response_to_anthropic(payload, model=model)
    else:
        result = payload
    headers = {COST_HEADER: repr(cost)}
    if cold_start_ms > 0:
        headers[COLD_START_HEADER] = str(cold_start_ms)
    return JSONResponse(result, status_code=200, headers=headers)


async def _emit_mismatch(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    model: str,
    got: Any,
) -> None:
    lock = entry.route.provider_lock
    await state.emit(
        EVENT_ENDPOINT_MISMATCH,
        trial_id=key.trial_id,
        endpoint_id=key.endpoint_id,
        model=model,
        response_field=lock.response_field if lock else "",
        pattern=lock.pattern if lock else "",
        got=got,
    )


async def _forward_stream(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    body: dict[str, Any],
    *,
    model: str,
    round_: int,
    started_at: float,
    flavor: Flavor,
    responses: bool,
) -> Response:
    try:
        opened = await open_upstream_stream(state, entry, body, endpoint_id=key.endpoint_id)
    except UpstreamCredentialError as exc:
        return _error_response(flavor, 502, "upstream_error", f"upstream credential: {exc}")
    except httpx.TransportError as exc:
        return await _transport_failure(state, key, entry, model, round_, started_at, flavor, exc)

    upstream = opened.response
    if upstream.status_code != 200:
        try:
            payload: dict[str, Any] = upstream.json()
        except ValueError:
            payload = {}
        await opened.aclose()
        await _record_usage(
            state,
            key=key,
            entry=entry,
            model=model,
            round_=round_,
            usage=None,
            latency_ms=_latency_ms(state, started_at),
            status_code=upstream.status_code,
            cold_start_ms=opened.cold_start_ms,
            charge=False,
        )
        message, code = _upstream_error_payload(payload)
        return _error_response(flavor, upstream.status_code, code, message)

    first_chunk = opened.first_chunk or {}
    if provider_lock_mismatch(entry.route, first_chunk):
        await opened.aclose()
        await _emit_mismatch(state, key, entry, model, first_chunk.get("model"))
        await _record_usage(
            state,
            key=key,
            entry=entry,
            model=model,
            round_=round_,
            usage=None,
            latency_ms=_latency_ms(state, started_at),
            status_code=502,
            cold_start_ms=opened.cold_start_ms,
            charge=False,
        )
        return _error_response(
            flavor, 502, "endpoint_mismatch", "upstream response violates the provider lock"
        )

    if flavor == "anthropic":
        return _converted_stream(
            state,
            key,
            entry,
            opened,
            ChatToAnthropicStream,
            model,
            round_,
            _upstream_model(first_chunk),
        )
    if responses:
        return _converted_stream(
            state,
            key,
            entry,
            opened,
            ChatToResponsesStream,
            model,
            round_,
            _upstream_model(first_chunk),
        )
    return _passthrough_stream(
        state, key, entry, opened, model, round_, started_at, _upstream_model(first_chunk)
    )


def _mid_stream_error(
    kind: Literal["chat", "anthropic", "responses"], exc: httpx.TransportError
) -> tuple[int, str]:
    """The status to record and the SSE error event to send when the upstream fails mid-stream.

    The status line has already gone out as 200, so the failure can only reach the client as an
    error event in the stream's own protocol; the OpenAI SDK raises on a chunk with ``error``.
    """
    timed_out = isinstance(exc, httpx.TimeoutException)
    status = 504 if timed_out else 502
    code = "upstream_timeout" if timed_out else "upstream_error"
    message = f"upstream {'timed out' if timed_out else 'connection failed'} mid-stream: {exc!r}"
    if kind == "anthropic":
        body = {"type": "error", "error": {"type": "api_error", "message": message}}
        return status, "event: error\ndata: " + json.dumps(body) + "\n\n"
    if kind == "responses":
        body = {"type": "error", "code": code, "message": message}
        return status, "event: error\ndata: " + json.dumps(body) + "\n\n"
    body = {"error": {"message": message, "type": "server_error", "code": code}}
    return status, "\ndata: " + json.dumps(body) + "\n\n"


def _upstream_model(chunk: dict[str, Any]) -> str:
    value = chunk.get("model")
    return value if isinstance(value, str) else ""


def _stream_headers(opened: OpenedStream) -> dict[str, str] | None:
    return {COLD_START_HEADER: str(opened.cold_start_ms)} if opened.cold_start_ms > 0 else None


def _passthrough_stream(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    opened: OpenedStream,
    model: str,
    round_: int,
    started_at: float,
    upstream_model: str,
) -> StreamingResponse:
    """Chat Completions streaming: forward SSE lines, then charge from the usage chunk."""

    async def generate() -> AsyncIterator[str]:
        usage_holder: dict[str, Any] = {}
        try:
            async for line in opened.iter_lines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    try:
                        chunk = json.loads(line[6:])
                    except ValueError:
                        chunk = None
                    if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                        usage_holder["usage"] = chunk["usage"]
                yield line + "\n"
        except httpx.TransportError as exc:
            usage_holder["status"], event = _mid_stream_error("chat", exc)
            yield event
        finally:
            await opened.aclose()
            await _record_usage(
                state,
                key=key,
                entry=entry,
                model=model,
                round_=round_,
                usage=usage_holder.get("usage"),
                latency_ms=_latency_ms(state, started_at),
                status_code=int(usage_holder.get("status", 200)),
                cold_start_ms=opened.cold_start_ms,
                charge=True,
                upstream_model=upstream_model,
            )

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers=_stream_headers(opened)
    )


def _converted_stream(
    state: GatewayState,
    key: StoredKey,
    entry: RouteEntry,
    opened: OpenedStream,
    converter_type: type[ChatToAnthropicStream] | type[ChatToResponsesStream],
    model: str,
    round_: int,
    upstream_model: str,
) -> StreamingResponse:
    """Wrap a raw upstream stream in a protocol converter (Anthropic or Responses)."""
    converter = converter_type(model=model)
    started_at = state.clock.now()

    async def generate() -> AsyncIterator[str]:
        usage_holder: dict[str, Any] = {}
        try:
            async for line in opened.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage_holder["usage"] = chunk["usage"]
                for event in converter.feed(chunk):
                    yield event
            for event in converter.finish():
                yield event
        except httpx.TransportError as exc:
            kind: Literal["anthropic", "responses"] = (
                "anthropic" if converter_type is ChatToAnthropicStream else "responses"
            )
            usage_holder["status"], event = _mid_stream_error(kind, exc)
            yield event
        finally:
            await opened.aclose()
            await _record_usage(
                state,
                key=key,
                entry=entry,
                model=model,
                round_=round_,
                usage=usage_holder.get("usage"),
                latency_ms=_latency_ms(state, started_at),
                status_code=int(usage_holder.get("status", 200)),
                cold_start_ms=opened.cold_start_ms,
                charge=True,
                upstream_model=upstream_model,
            )

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers=_stream_headers(opened)
    )
