"""Data-plane tests through the FastAPI app with a scripted upstream transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from evalspleef.gateway.data_app import create_data_app
from evalspleef.gateway.state import (
    COLD_START_HEADER,
    EVENT_BUDGET_REJECTED,
    EVENT_ENDPOINT_COLD_START,
    EVENT_ENDPOINT_MISMATCH,
    GatewayState,
    hash_secret,
)
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP, PriceSpec
from evalspleef.spi.model import ProviderLock, UpstreamRoute
from evalspleef.testing.secrets import DictSecretResolver

from .conftest import EXPIRES_AT, TEST_ROUTE, UPSTREAM_KEY, auth

Handler = Callable[[httpx.Request], httpx.Response]


def canned_chat(
    content: str | None = "fake reply",
    *,
    model: str = "fake-model",
    prompt_tokens: int = 11,
    completion_tokens: int = 2,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def ok_upstream(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    return httpx.Response(200, json=canned_chat())


def sse_body(*payloads: dict[str, Any]) -> bytes:
    chunks = "".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads)
    return (chunks + "data: [DONE]\n\n").encode()


async def issue(state: GatewayState, endpoint_id: str = "ep1", **overrides: Any) -> str:
    params: dict[str, Any] = {
        "trial_id": f"trial-{endpoint_id}",
        "endpoint_id": endpoint_id,
        "max_budget_usd": 100.0,
        "max_tokens": 10_000_000,
        "expires_at": EXPIRES_AT,
    }
    params.update(overrides)
    key = await state.issue_key(**params)
    return key.secret


def add_route(
    state: GatewayState,
    name: str = "ep_ep1",
    *,
    overrides: dict[str, Any] | None = None,
) -> None:
    raw = dict(TEST_ROUTE)
    raw.update(overrides or {})
    lock = raw.pop("provider_lock", None)
    route = UpstreamRoute(
        api_base=raw["api_base"],
        model=raw["model"],
        protocol=raw["protocol"],
        api_key_ref=raw["api_key_ref"],
        default_params=raw.get("default_params", {}),
        extra_headers=raw.get("extra_headers", {}),
        provider_lock=ProviderLock(**lock) if lock else None,
        cold_start_window_s=raw.get("cold_start_window_s", 0),
    )
    state.upsert_route(name, route)


def client_for(state: GatewayState, handler: Handler) -> httpx.AsyncClient:
    """Point the state's upstream client at the scripted transport and return a test client."""
    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_data_app(state)), base_url="http://gw.test"
    )


# -- authentication and routing --------------------------------------------------------


async def test_missing_and_unknown_keys_get_401(state: GatewayState) -> None:
    add_route(state)
    async with client_for(state, ok_upstream) as client:
        response = await client.post("/v1/chat/completions", json={"model": "ep_ep1"})
        assert response.status_code == 401
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1"},
            headers=auth("sk-trial-unknown"),
        )
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"


async def test_healthz_and_models(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)
    async with client_for(state, ok_upstream) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        response = await client.get("/v1/models", headers=auth(secret))
        assert response.status_code == 200
        assert [model["id"] for model in response.json()["data"]] == ["ep_ep1"]

        response = await client.get("/v1/models")
        assert response.status_code == 401


async def test_unknown_model_gets_404(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_nope"},
            headers=auth(secret),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"


async def test_key_bound_to_other_endpoint_gets_403(state: GatewayState) -> None:
    add_route(state)
    add_route(state, "ep_ep2", overrides={"model": "other-model"})
    secret = await issue(state, endpoint_id="ep1")
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep2", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(secret),
        )
        assert response.status_code == 403


# -- chat completions -----------------------------------------------------------------


async def test_chat_non_stream_passthrough_and_usage(state: GatewayState) -> None:
    add_route(state)
    state.set_price("ep_ep1", PriceSpec(input_per_mtok=1.0, output_per_mtok=2.0))
    secret = await issue(state)
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(secret),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "fake-model"
        assert body["choices"][0]["message"]["content"] == "fake reply"
        assert "x-spleef-gateway-version" in response.headers

        usage = await state.usage_for("trial-ep1")
        assert len(usage) == 1
        record = usage[0]
        assert record.input_tokens == 11 and record.output_tokens == 2
        assert record.upstream_model == "fake-model"
        # 11 * $1/M + 2 * $2/M
        assert record.cost_usd == pytest.approx(0.000015)
        stored = await state.key_store.key_by_secret_hash(hash_secret(secret))
        assert stored is not None
        assert stored.spent_tokens == 13
        assert stored.spent_cost_usd == pytest.approx(0.000015)


async def test_chat_stream_passthrough_and_usage(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {"content": "fake"}, "finish_reason": None}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body(*chunks))

    async with client_for(state, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "ep_ep1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth(secret),
        )
        assert response.status_code == 200
        text = response.text
        assert "data: [DONE]" in text
        usage = await state.usage_for("trial-ep1")
        assert len(usage) == 1
        assert usage[0].input_tokens == 5 and usage[0].output_tokens == 1


async def test_default_params_apply_ceiling_and_usage_option(state: GatewayState) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=canned_chat())

    add_route(state, overrides={"default_params": {"max_tokens": 64, "temperature": 0.0}})
    secret = await issue(state)
    async with client_for(state, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "ep_ep1",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 500,
                "temperature": 0.7,
            },
            headers=auth(secret),
        )
        assert response.status_code == 200
        # Body wins over route defaults, but max_tokens is a ceiling.
        assert seen[0]["max_tokens"] == 64
        assert seen[0]["temperature"] == 0.7

        await client.post(
            "/v1/chat/completions",
            json={
                "model": "ep_ep1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth(secret),
        )
        assert seen[1]["stream_options"]["include_usage"] is True


async def test_upstream_error_is_relayed_and_recorded(state: GatewayState) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "bad request",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            },
        )

    add_route(state)
    secret = await issue(state)
    async with client_for(state, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(secret),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "context_length_exceeded"
        usage = await state.usage_for("trial-ep1")
        assert len(usage) == 1 and usage[0].status_code == 400 and usage[0].cost_usd == 0.0


# -- budgets --------------------------------------------------------------------------


async def test_cost_cap_rejection_with_429(state: GatewayState) -> None:
    add_route(state)
    state.set_price("ep_ep1", PriceSpec(input_per_mtok=1.0, output_per_mtok=1.0))
    secret = await issue(state, max_budget_usd=0.000001)  # below the projected minimum
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(secret),
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == COST_CAP
        events = state.events()
        assert any(event["type"] == EVENT_BUDGET_REJECTED for event in events)
        usage = await state.usage_for("trial-ep1")
        assert usage[0].status_code == 429


async def test_token_cap_rejection_with_429(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state, max_tokens=10)
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "ep_ep1",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 500,
            },
            headers=auth(secret),
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == TOKEN_CAP


async def test_revoked_key_gets_401(state: GatewayState) -> None:
    add_route(state)
    key = await state.issue_key(
        trial_id="trial-ep1",
        endpoint_id="ep1",
        max_budget_usd=1.0,
        max_tokens=100,
        expires_at=EXPIRES_AT,
    )
    await state.revoke_key(key.key_id)
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(key.secret),
        )
        assert response.status_code == 401


# -- anthropic and responses ------------------------------------------------------------

ANTHROPIC_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def tool_call_upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    messages = body.get("messages", [])
    if any(message.get("role") == "tool" for message in messages):
        return httpx.Response(
            200, json=canned_chat("sunny in Berlin", prompt_tokens=20, completion_tokens=4)
        )
    payload = canned_chat(content=None, prompt_tokens=8, completion_tokens=6)
    payload["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Berlin"})},
        }
    ]
    payload["choices"][0]["finish_reason"] = "tool_calls"
    return httpx.Response(200, json=payload)


async def test_anthropic_tool_round_trip_non_stream(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)
    async with client_for(state, tool_call_upstream) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "ep_ep1",
                "max_tokens": 100,
                "tools": ANTHROPIC_TOOLS,
                "messages": [{"role": "user", "content": "weather in Berlin?"}],
            },
            headers=auth(secret),
        )
        assert response.status_code == 200
        message = response.json()
        assert message["model"] == "ep_ep1"
        tool_blocks = [b for b in message["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["name"] == "get_weather"
        assert tool_blocks[0]["input"] == {"city": "Berlin"}
        assert message["stop_reason"] == "tool_use"

        # x-api-key auth also works, and the tool_result round trip answers with text.
        response = await client.post(
            "/v1/messages",
            json={
                "model": "ep_ep1",
                "max_tokens": 100,
                "tools": ANTHROPIC_TOOLS,
                "messages": [
                    {"role": "user", "content": "weather in Berlin?"},
                    {"role": "assistant", "content": tool_blocks},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_blocks[0]["id"],
                                "content": "sunny",
                            }
                        ],
                    },
                ],
            },
            headers={"x-api-key": secret},
        )
        assert response.status_code == 200
        text = "".join(b["text"] for b in response.json()["content"] if b["type"] == "text")
        assert "sunny in Berlin" in text


def _tool_stream_response(with_tool: bool) -> httpx.Response:
    if with_tool:
        delta = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Berlin"}),
                    },
                }
            ],
        }
        finish = "tool_calls"
        usage = {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14}
    else:
        delta = {"role": "assistant", "content": "sunny in Berlin"}
        finish = "stop"
        usage = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [],
            "usage": usage,
        },
    ]
    return httpx.Response(200, content=sse_body(*chunks))


async def test_anthropic_stream_tool_round_trip(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        return _tool_stream_response(not has_tool_result)

    async with client_for(state, handler) as client:
        for round_index, expect_tool in [(0, True), (1, False)]:
            messages: list[Any] = [{"role": "user", "content": "weather in Berlin?"}]
            if round_index == 1:
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_0",
                                "name": "get_weather",
                                "input": {"city": "Berlin"},
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call_0", "content": "sunny"}
                        ],
                    }
                )
            response = await client.post(
                "/v1/messages",
                json={
                    "model": "ep_ep1",
                    "max_tokens": 100,
                    "stream": True,
                    "tools": ANTHROPIC_TOOLS,
                    "messages": messages,
                },
                headers=auth(secret),
            )
            assert response.status_code == 200
            events = _parse_sse(response.text)
            if expect_tool:
                starts = [
                    e
                    for e in events
                    if e.get("type") == "content_block_start"
                    and e["content_block"]["type"] == "tool_use"
                ]
                assert starts, f"round {round_index}: {response.text}"
                assert starts[0]["content_block"]["name"] == "get_weather"
            else:
                deltas = [e for e in events if e.get("type") == "content_block_delta"]
                text = "".join(e["delta"]["text"] for e in deltas)
                assert "sunny in Berlin" in text


async def test_responses_endpoint_non_stream(state: GatewayState) -> None:
    add_route(state)
    secret = await issue(state)
    async with client_for(state, ok_upstream) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "ep_ep1", "input": "say hello"},
            headers=auth(secret),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "ep_ep1"
        assert body["status"] == "completed"
        text = "".join(
            item.get("content", [{}])[0].get("text", "")
            for item in body.get("output", [])
            if item.get("type") == "message"
        )
        assert "fake reply" in text


# -- provider lock and cold start -------------------------------------------------------


async def test_provider_lock_mismatch_returns_502(state: GatewayState) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned_chat(model="evil-aggregator-model"))

    add_route(
        state, overrides={"provider_lock": {"response_field": "model", "pattern": "^fake-model$"}}
    )
    secret = await issue(state)
    async with client_for(state, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(secret),
        )
        assert response.status_code == 502
        usage = await state.usage_for("trial-ep1")
        assert usage[0].status_code == 502  # flagged on the usage record
        events = state.events()
        mismatch = [e for e in events if e["type"] == EVENT_ENDPOINT_MISMATCH]
        assert len(mismatch) == 1
        assert mismatch[0]["got"] == "evil-aggregator-model"


async def test_cold_start_absorbs_503_with_injected_clock(state: GatewayState) -> None:
    from evalspleef.testing.clock import FakeClock

    clock = FakeClock(start=1000.0)
    calls = 0
    call_observed = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        call_observed.set()
        if calls < 3:
            return httpx.Response(
                503, json={"error": {"message": "sleeping", "code": "service_unavailable"}}
            )
        return httpx.Response(200, json=canned_chat())

    store = MemoryStore()
    cold_state = GatewayState(
        key_store=store,
        usage_store=store,
        secrets=DictSecretResolver({"vault://upstream/key": UPSTREAM_KEY}),
        clock=clock,
        cold_start_interval_s=15.0,
    )
    add_route(cold_state, overrides={"cold_start_window_s": 45})
    secret = await issue(cold_state)

    async with client_for(cold_state, handler) as client:
        task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth(secret),
            )
        )
        # Drive the FakeClock past each retry without waiting on wall time.
        for _ in range(2):
            await asyncio.wait_for(call_observed.wait(), timeout=5)
            call_observed.clear()
            clock.advance(15.0)
        response = await asyncio.wait_for(task, timeout=5)

    assert response.status_code == 200
    assert response.headers[COLD_START_HEADER] == "30000"
    usage = await cold_state.usage_for("trial-ep1")
    assert usage[0].cold_start_ms == 30000
    events = cold_state.events()
    cold = [e for e in events if e["type"] == EVENT_ENDPOINT_COLD_START]
    assert len(cold) == 1


async def _post_with_fake_clock(
    handler: Callable[[httpx.Request], httpx.Response], *, stream: bool
) -> tuple[httpx.Response, list[float]]:
    """POST one chat request through a gateway whose clock records every backoff wait."""
    from evalspleef.testing.clock import FakeClock

    waits: list[float] = []

    class RecordingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            waits.append(seconds)
            self.advance(seconds)

    store = MemoryStore()
    backoff_state = GatewayState(
        key_store=store,
        usage_store=store,
        secrets=DictSecretResolver({"vault://upstream/key": UPSTREAM_KEY}),
        clock=RecordingClock(start=1000.0),
    )
    add_route(backoff_state)
    secret = await issue(backoff_state)
    async with client_for(backoff_state, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]}
            | ({"stream": True} if stream else {}),
            headers=auth(secret),
        )
        await response.aread()
    return response, waits


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_rate_limits_are_retried_with_backoff(stream: bool) -> None:
    """Novita answered 429 to a burst of probe requests; the gateway now waits and retries."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        if stream:
            chunk = {
                "id": "c1",
                "model": "fake-model",
                "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
            return httpx.Response(200, content=sse_body(chunk))
        return httpx.Response(200, json=canned_chat())

    response, waits = await _post_with_fake_clock(handler, stream=stream)
    assert response.status_code == 200
    assert (calls, waits) == (3, [1.0, 2.0])


async def test_retry_after_is_honoured_and_retries_are_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"retry-after": "7"}, json={"error": {}})

    response, waits = await _post_with_fake_clock(handler, stream=False)
    # Five retries after the first attempt, then the upstream status reaches the caller.
    assert (response.status_code, calls, waits) == (503, 6, [7.0] * 5)


# -- concurrency bound ------------------------------------------------------------------


async def test_concurrent_requests_cannot_overspend_beyond_in_flight(state: GatewayState) -> None:
    """Five concurrent requests against a 250-token budget: at most the in-flight set
    that fit at admission completes, and the overshoot stays below one request's full
    cost (the un-projected input tokens)."""
    budget_tokens = 250
    projected_per_request = 100  # requested max_tokens
    actual_tokens = 150  # 50 input tokens are not part of the projection
    arrivals = 0
    all_arrived = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        # Hold the admitted requests in flight until both arrived upstream.
        nonlocal arrivals
        arrivals += 1
        if arrivals >= 2:
            all_arrived.set()
        else:
            await asyncio.wait_for(all_arrived.wait(), timeout=5)
        return httpx.Response(200, json=canned_chat(prompt_tokens=50, completion_tokens=100))

    add_route(state)
    secret = await issue(state, max_tokens=budget_tokens)
    async with client_for(state, handler) as client:

        async def one() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                json={
                    "model": "ep_ep1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": projected_per_request,
                },
                headers=auth(secret),
            )

        tasks = [asyncio.create_task(one()) for _ in range(5)]
        responses = await asyncio.gather(*tasks)

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 200, 429, 429, 429]
    stored = await state.key_store.key_by_secret_hash(hash_secret(secret))
    assert stored is not None
    # Two requests were admitted (2 * 100 <= 250); their real usage is 2 * 150 = 300,
    # which exceeds the budget only by the un-projected part of the in-flight set.
    assert stored.spent_tokens == 2 * actual_tokens
    assert (
        stored.spent_tokens
        <= budget_tokens + actual_tokens  # never more than one in-flight request over
    )


# -- helpers ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        data_lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        if isinstance(payload, dict):
            events.append(payload)
    return events
