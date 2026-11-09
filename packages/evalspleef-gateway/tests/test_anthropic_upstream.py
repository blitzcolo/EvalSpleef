"""Data-plane tests for a route whose upstream speaks Anthropic Messages."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from evalspleef.gateway.state import (
    DROPPED_PARAMS_HEADER,
    EVENT_ENDPOINT_COLD_START,
    GatewayState,
    compute_cost,
)
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.gateway import COST_HEADER, PriceSpec
from evalspleef.testing.secrets import DictSecretResolver

from .conftest import UPSTREAM_KEY, auth
from .test_data_app import Handler, add_route, client_for, issue

#: Claude Sonnet 5.5, USD per million tokens (input, 5-minute cache write, cache read, output).
SONNET_PRICE = PriceSpec(
    input_per_mtok=2.0, output_per_mtok=10.0, cached_per_mtok=0.10, cache_write_per_mtok=2.5
)
CHAT = {
    "model": "ep_ep1",
    "messages": [{"role": "user", "content": "grade this"}],
    "max_tokens": 64,
    "temperature": 0.0,
}


def anthropic_reply(**usage: int) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "verdict"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 200} | usage,
    }


def anthropic_route(**default_params: Any) -> dict[str, Any]:
    return {
        "protocol": "anthropic_messages",
        "model": "claude-sonnet-5-5",
        "default_params": default_params,
    }


def recording(seen: list[httpx.Request], reply: Callable[[int], httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(len(seen))

    return handler


async def post(
    state: GatewayState, handler: Handler, body: dict[str, Any] | None = None
) -> httpx.Response:
    secret = await issue(state)
    async with client_for(state, handler) as client:
        return await client.post("/v1/chat/completions", json=body or CHAT, headers=auth(secret))


async def test_request_url_headers_and_body_follow_the_messages_api(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route())
    seen: list[httpx.Request] = []
    response = await post(
        state, recording(seen, lambda _: httpx.Response(200, json=anthropic_reply()))
    )

    assert response.status_code == 200
    (request,) = seen
    assert str(request.url) == "http://upstream.test/v1/messages"
    assert request.headers["x-api-key"] == UPSTREAM_KEY
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert "authorization" not in request.headers
    sent = json.loads(request.content)
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["messages"] == [{"role": "user", "content": "grade this"}]
    assert response.json()["choices"][0]["message"]["content"] == "verdict"


async def test_extra_headers_may_override_the_api_version(state: GatewayState) -> None:
    add_route(
        state,
        overrides=anthropic_route() | {"extra_headers": {"Anthropic-Version": "2099-01-01"}},
    )
    seen: list[httpx.Request] = []
    await post(state, recording(seen, lambda _: httpx.Response(200, json=anthropic_reply())))
    assert seen[0].headers.get_list("anthropic-version") == ["2099-01-01"]


@pytest.mark.parametrize(
    ("caching", "expected"),
    [
        (None, {"type": "ephemeral"}),
        (True, {"type": "ephemeral"}),
        ("1h", {"type": "ephemeral", "ttl": "1h"}),
        (False, None),
    ],
)
async def test_prompt_caching_route_key(state: GatewayState, caching: Any, expected: Any) -> None:
    params = {} if caching is None else {"prompt_caching": caching}
    add_route(state, overrides=anthropic_route(**params))
    seen: list[httpx.Request] = []
    await post(state, recording(seen, lambda _: httpx.Response(200, json=anthropic_reply())))
    sent = json.loads(seen[0].content)
    assert sent.get("cache_control") == expected
    assert "prompt_caching" not in sent


async def test_drop_params_removes_keys_and_reports_them(state: GatewayState) -> None:
    add_route(
        state,
        overrides=anthropic_route(
            drop_params=["temperature", "top_p"], output_config={"effort": "high"}
        ),
    )
    seen: list[httpx.Request] = []
    response = await post(
        state, recording(seen, lambda _: httpx.Response(200, json=anthropic_reply()))
    )
    sent = json.loads(seen[0].content)
    assert "temperature" not in sent and "drop_params" not in sent
    assert sent["output_config"] == {"effort": "high"}
    # top_p was never sent, so only temperature counts as dropped.
    assert response.headers[DROPPED_PARAMS_HEADER] == "temperature"


async def test_no_dropped_header_when_nothing_was_removed(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route(drop_params=["top_k"]))
    response = await post(
        state, recording([], lambda _: httpx.Response(200, json=anthropic_reply()))
    )
    assert DROPPED_PARAMS_HEADER not in response.headers


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"n": 2},
        {"logprobs": True},
        {"logit_bias": {"1": 1}},
        {"seed": 7},
        {"frequency_penalty": 0.5},
        {"presence_penalty": 0.5},
        {"user": "u"},
        {"messages": [{"role": "tool", "content": "x", "tool_call_id": "t"}]},
        {"messages": [{"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
    ],
)
async def test_unsupported_fields_are_a_400_and_never_reach_the_upstream(
    state: GatewayState, extra: dict[str, Any]
) -> None:
    add_route(state, overrides=anthropic_route())
    seen: list[httpx.Request] = []
    response = await post(
        state, recording(seen, lambda _: httpx.Response(200, json=anthropic_reply())), CHAT | extra
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert seen == []


async def test_anthropic_client_tools_are_rejected_in_its_own_envelope(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    seen: list[httpx.Request] = []
    secret = await issue(state)
    body = {
        "model": "ep_ep1",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "f", "input_schema": {"type": "object"}}],
    }
    handler = recording(seen, lambda _: httpx.Response(200, json=anthropic_reply()))
    async with client_for(state, handler) as client:
        response = await client.post("/v1/messages", json=body, headers=auth(secret))
        assert response.status_code == 400
        assert response.json()["type"] == "error"
        assert "tools" in response.json()["error"]["message"]
        assert seen == []

        body.pop("tools")
        ok = await client.post("/v1/messages", json=body, headers=auth(secret))
    assert ok.status_code == 200
    assert ok.json()["content"][0]["text"] == "verdict"


async def test_cache_write_then_read_is_priced_and_recorded(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route())
    state.set_price("ep_ep1", SONNET_PRICE)
    usages = [
        {"cache_creation_input_tokens": 3750},
        {"cache_read_input_tokens": 3750},
    ]
    seen: list[httpx.Request] = []
    handler = recording(seen, lambda n: httpx.Response(200, json=anthropic_reply(**usages[n - 1])))
    secret = await issue(state)
    async with client_for(state, handler) as client:
        first = await client.post("/v1/chat/completions", json=CHAT, headers=auth(secret))
        second = await client.post("/v1/chat/completions", json=CHAT, headers=auth(secret))

    # (10 * 2.00 + 3750 * 2.50 + 200 * 10.00) / 1e6
    assert float(first.headers[COST_HEADER]) == pytest.approx(0.011395)
    # (10 * 2.00 + 3750 * 0.10 + 200 * 10.00) / 1e6
    assert float(second.headers[COST_HEADER]) == pytest.approx(0.002395)
    written, read = await state.usage_for("trial-ep1")
    assert (written.input_tokens, written.cached_tokens, written.output_tokens) == (3760, 0, 200)
    assert (read.input_tokens, read.cached_tokens, read.output_tokens) == (3760, 3750, 200)
    assert written.cost_usd == pytest.approx(0.011395)
    assert written.upstream_model == "claude-sonnet-5-5"


def test_compute_cost_prices_cache_writes_separately() -> None:
    assert compute_cost(
        SONNET_PRICE, input_tokens=3760, output_tokens=200, cached_tokens=0, cache_write_tokens=3750
    ) == pytest.approx(0.011395)
    assert compute_cost(
        SONNET_PRICE, input_tokens=3760, output_tokens=200, cached_tokens=3750
    ) == pytest.approx(0.002395)


def test_cache_writes_default_to_the_input_price() -> None:
    plain = PriceSpec(input_per_mtok=2.0, output_per_mtok=10.0, cached_per_mtok=0.10)
    # (3760 * 2.00 + 200 * 10.00) / 1e6
    assert compute_cost(
        plain, input_tokens=3760, output_tokens=200, cached_tokens=0, cache_write_tokens=3750
    ) == pytest.approx(0.00952)


async def test_upstream_errors_keep_message_and_type(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route())
    error = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "temperature: Extra inputs"},
    }
    response = await post(state, recording([], lambda _: httpx.Response(400, json=error)))
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "temperature: Extra inputs"
    assert response.json()["error"]["code"] == "invalid_request_error"
    (record,) = await state.usage_for("trial-ep1")
    assert (record.status_code, record.cost_usd) == (400, 0.0)


async def test_overload_529_is_retried_then_surfaced() -> None:
    from evalspleef.testing.clock import FakeClock

    waits: list[float] = []

    class RecordingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            waits.append(seconds)
            self.advance(seconds)

    store = MemoryStore()
    state = GatewayState(
        key_store=store,
        usage_store=store,
        secrets=DictSecretResolver({"vault://upstream/key": UPSTREAM_KEY}),
        clock=RecordingClock(start=1000.0),
    )
    add_route(state, overrides=anthropic_route())
    seen: list[httpx.Request] = []
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = await post(state, recording(seen, lambda _: httpx.Response(529, json=overloaded)))
    assert response.status_code == 529
    assert response.json()["error"]["message"] == "Overloaded"
    assert response.json()["error"]["code"] == "overloaded_error"
    # First attempt plus the five bounded retries.
    assert (len(seen), len(waits)) == (6, 5)


async def test_provider_lock_checks_the_upstream_payload(state: GatewayState) -> None:
    add_route(
        state,
        overrides=anthropic_route()
        | {"provider_lock": {"response_field": "model", "pattern": "^claude-"}},
    )
    ok = await post(state, recording([], lambda _: httpx.Response(200, json=anthropic_reply())))
    assert ok.status_code == 200
    reply = anthropic_reply() | {"model": "other-vendor"}
    bad = await post(state, recording([], lambda _: httpx.Response(200, json=reply)))
    assert bad.status_code == 502
    assert bad.json()["error"]["code"] == "endpoint_mismatch"


async def test_cold_start_event_names_the_messages_url() -> None:
    from evalspleef.testing.clock import FakeClock

    class AdvancingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            self.advance(seconds)

    store = MemoryStore()
    state = GatewayState(
        key_store=store,
        usage_store=store,
        secrets=DictSecretResolver({"vault://upstream/key": UPSTREAM_KEY}),
        clock=AdvancingClock(start=1000.0),
    )
    add_route(state, overrides=anthropic_route() | {"cold_start_window_s": 60})
    reply = lambda n: (  # noqa: E731
        httpx.Response(503, json={}) if n == 1 else httpx.Response(200, json=anthropic_reply())
    )
    response = await post(state, recording([], reply))
    assert response.status_code == 200
    (event,) = [e for e in state.events() if e["type"] == EVENT_ENDPOINT_COLD_START]
    assert event["url"] == "http://upstream.test/v1/messages"
