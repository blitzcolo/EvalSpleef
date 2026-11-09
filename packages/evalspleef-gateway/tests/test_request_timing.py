"""Passive per-request timing (TTFT, ITL, TPOT, E2E) and the per-route upstream timeout.

The fake upstream advances a FakeClock before it hands over each chunk and the gateway pulls
the upstream body lazily, so every figure below is exact and no test waits on wall time.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.data_app import create_data_app
from evalspleef.gateway.state import EVENT_REQUEST_COMPLETED, GatewayState
from evalspleef.gateway.store.sqlite import SqliteStore
from evalspleef.testing.clock import FakeClock

from .conftest import auth, make_state
from .test_data_app import add_route, canned_chat, client_for, issue

Step = tuple[float, str]
"""Seconds to advance the clock, then the raw SSE text the upstream hands over."""


def chat_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


class TimedBody(httpx.AsyncByteStream):
    """An upstream body that advances the clock before yielding each step."""

    def __init__(self, clock: FakeClock, steps: Sequence[Step]) -> None:
        self._clock = clock
        self._steps = steps

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, text in self._steps:
            self._clock.advance(delay)
            yield text.encode()


USAGE = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}

# Request reaches the gateway at t=0. Role chunk at 0.2 s (no text), "Hel" at 0.5 s,
# "lo" at 0.6 s, " world" at 0.85 s, finish chunk at 0.9 s, usage chunk at 0.9 s.
CHAT_STEPS: tuple[Step, ...] = (
    (0.2, data(chat_chunk({"role": "assistant"}))),
    (0.3, data(chat_chunk({"content": "Hel"}))),
    (0.1, data(chat_chunk({"content": "lo"}))),
    (0.25, data(chat_chunk({"content": " world"}))),
    (0.05, data(chat_chunk({}, "stop"))),
    (0.0, data({**chat_chunk({}), "choices": [], "usage": USAGE})),
    (0.0, "data: [DONE]\n\n"),
)

# TTFT 500 ms; gaps 100 and 250 ms: mean 175, p95 100 + 150 * 0.95 = 242.5, max 250;
# TPOT (0.85 - 0.5) / (3 - 1) = 175 ms; E2E 900 ms.
EXPECTED = {
    "ttft_ms": 500.0,
    "e2e_ms": 900.0,
    "tpot_ms": 175.0,
    "itl_mean_ms": 175.0,
    "itl_p95_ms": 242.5,
    "itl_max_ms": 250.0,
    "itl_count": 2,
}


def timed_state() -> tuple[GatewayState, FakeClock]:
    clock = FakeClock()
    return make_state(clock=clock), clock


def streaming(clock: FakeClock, steps: Sequence[Step]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=TimedBody(clock, steps))

    return handler


def timing_of(record: Any) -> dict[str, Any]:
    return {name: getattr(record, name) for name in EXPECTED}


STREAM_BODY = {
    "model": "ep_ep1",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": True,
}


async def test_streamed_chat_records_ttft_itl_tpot_and_e2e_from_the_first_text_chunk() -> None:
    state, clock = timed_state()
    add_route(state)
    secret = await issue(state)
    async with client_for(state, streaming(clock, CHAT_STEPS)) as client:
        response = await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth(secret))
        assert response.status_code == 200
        assert "Hel" in response.text
    (record,) = await state.usage_for("trial-ep1")
    assert timing_of(record) == EXPECTED
    assert record.output_tokens == 3
    completed = [e for e in state.events() if e["type"] == EVENT_REQUEST_COMPLETED]
    assert {name: completed[-1][name] for name in EXPECTED} == EXPECTED


async def test_reasoning_chunks_count_as_generated_text() -> None:
    state, clock = timed_state()
    add_route(state)
    secret = await issue(state)
    steps = (
        (0.4, data(chat_chunk({"reasoning_content": "thinking"}))),
        (0.2, data(chat_chunk({"content": "ok"}))),
        (0.0, data({**chat_chunk({}), "choices": [], "usage": USAGE | {"completion_tokens": 2}})),
        (0.0, "data: [DONE]\n\n"),
    )
    async with client_for(state, streaming(clock, steps)) as client:
        await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth(secret))
    (record,) = await state.usage_for("trial-ep1")
    assert (record.ttft_ms, record.itl_count, record.itl_max_ms) == (400.0, 1, 200.0)
    assert record.tpot_ms == 200.0


async def test_a_non_streamed_request_has_e2e_but_no_ttft_itl_or_tpot() -> None:
    state, clock = timed_state()
    add_route(state)
    secret = await issue(state)

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(1.5)
        return httpx.Response(200, json=canned_chat())

    async with client_for(state, handler) as client:
        body = {"model": "ep_ep1", "messages": [{"role": "user", "content": "hi"}]}
        response = await client.post("/v1/chat/completions", json=body, headers=auth(secret))
        assert response.status_code == 200
    (record,) = await state.usage_for("trial-ep1")
    assert record.e2e_ms == 1500.0
    assert (record.ttft_ms, record.tpot_ms, record.itl_mean_ms, record.itl_count) == (
        None,
        None,
        None,
        None,
    )


async def test_an_anthropic_client_stream_is_timed_on_the_upstream_chunks() -> None:
    state, clock = timed_state()
    add_route(state)
    secret = await issue(state)
    body = {
        "model": "ep_ep1",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    async with client_for(state, streaming(clock, CHAT_STEPS)) as client:
        response = await client.post("/v1/messages", json=body, headers=auth(secret))
        assert response.status_code == 200
    (record,) = await state.usage_for("trial-ep1")
    assert timing_of(record) == EXPECTED


def anthropic_event(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def text_delta(text: str) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }


async def test_an_anthropic_upstream_stream_is_timed_on_its_text_deltas() -> None:
    state, clock = timed_state()
    add_route(state, overrides={"protocol": "anthropic_messages"})
    secret = await issue(state)
    start = {
        "type": "message_start",
        "message": {
            "model": "fake-model",
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 11, "output_tokens": 1},
        },
    }
    steps = (
        (0.3, anthropic_event(start)),
        (
            0.0,
            anthropic_event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
        ),
        (0.4, anthropic_event(text_delta("one"))),
        (0.2, anthropic_event(text_delta(" two"))),
        (0.0, anthropic_event({"type": "content_block_stop", "index": 0})),
        (
            0.1,
            anthropic_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                }
            ),
        ),
        (0.0, anthropic_event({"type": "message_stop"})),
    )
    async with client_for(state, streaming(clock, steps)) as client:
        response = await client.post(
            "/v1/chat/completions", json=STREAM_BODY | {"max_tokens": 64}, headers=auth(secret)
        )
        assert response.status_code == 200
    (record,) = await state.usage_for("trial-ep1")
    assert (record.ttft_ms, record.itl_count, record.itl_mean_ms) == (700.0, 1, 200.0)
    assert (record.tpot_ms, record.e2e_ms) == (200.0, 1000.0)


async def test_admin_usage_exposes_the_timing_fields() -> None:
    state, clock = timed_state()
    add_route(state)
    secret = await issue(state)
    async with client_for(state, streaming(clock, CHAT_STEPS)) as client:
        await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth(secret))
    admin = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(state, "admin-token")),
        base_url="http://gw-admin.test",
    )
    async with admin:
        response = await admin.get(
            "/admin/usage", params={"trial_id": "trial-ep1"}, headers=auth("admin-token")
        )
    (record,) = response.json()
    assert {name: record[name] for name in EXPECTED} == EXPECTED


async def test_a_route_upstream_timeout_overrides_the_default_and_is_never_sent() -> None:
    state = make_state()
    assert state.client.timeout.read == 120.0  # the gateway default when a route sets none
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=canned_chat())

    async with client_for(state, handler) as client:
        state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=httpx.Timeout(120.0, connect=10.0)
        )
        add_route(state, "ep_slow", overrides={"default_params": {"upstream_timeout_s": 600}})
        add_route(state, "ep_plain")
        body = {"messages": [{"role": "user", "content": "hi"}]}
        for name in ("ep_slow", "ep_plain"):
            secret = await issue(state, name)
            response = await client.post(
                "/v1/chat/completions", json=body | {"model": name}, headers=auth(secret)
            )
            assert response.status_code == 200
    slow, plain = seen
    assert slow.extensions["timeout"]["read"] == 600.0
    assert slow.extensions["timeout"]["connect"] == 10.0
    assert "upstream_timeout_s" not in json.loads(slow.content)
    assert plain.extensions["timeout"]["read"] == 120.0


class OpenAfterDone(httpx.AsyncByteStream):
    """Hands over its steps, then keeps the connection open like a slow upstream close."""

    def __init__(self, clock: FakeClock, steps: Sequence[Step]) -> None:
        self._inner = TimedBody(clock, steps)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk
        await asyncio.Event().wait()


async def test_a_client_hanging_up_after_done_still_gets_its_request_recorded_and_charged(
    tmp_path: Path,
) -> None:
    """uvicorn declares ASGI 2.3, so Starlette cancels the response when the client disconnects,
    and anyio re-raises the cancellation at every await of the stream's cleanup. With a store
    that really suspends (SQLite runs in a thread), the usage record and the charge were lost."""
    clock = FakeClock()
    store = SqliteStore(str(tmp_path / "gateway.sqlite3"))
    state = make_state(clock=clock, key_store=store, usage_store=store)
    add_route(state)
    secret = await issue(state)
    state.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=OpenAfterDone(clock, CHAT_STEPS))
        )
    )
    saw_done = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": json.dumps(STREAM_BODY).encode(),
                "more_body": False,
            }
        await saw_done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        body = message.get("body")
        if isinstance(body, bytes) and b"[DONE]" in body:
            saw_done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {secret}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gw.test", 80),
    }
    await asyncio.wait_for(create_data_app(state)(scope, receive, send), timeout=5)
    await asyncio.wait_for(state.settle(), timeout=5)
    (record,) = await state.usage_for("trial-ep1")
    assert record.ttft_ms == 500.0 and record.output_tokens == 3
    key = await state.authenticate(secret)
    assert key is not None and key.spent_tokens == 14  # 11 prompt + 3 completion tokens


def tool_call(index_fields: dict[str, Any]) -> dict[str, Any]:
    return chat_chunk({"tool_calls": [{"index": 0, **index_fields}]})


# A turn that answers only with a tool call: the name at 0.5 s, argument fragments at 0.6 s
# and 0.85 s, then finish and usage. Same timing as CHAT_STEPS, so the same figures.
TOOL_CALL_STEPS: tuple[Step, ...] = (
    (0.2, data(chat_chunk({"role": "assistant"}))),
    (
        0.3,
        data(
            tool_call(
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": ""}}
            )
        ),
    ),
    (0.1, data(tool_call({"function": {"arguments": '{"command": '}}))),
    (0.25, data(tool_call({"function": {"arguments": '"ls"}'}}))),
    (0.05, data(chat_chunk({}, "tool_calls"))),
    (0.0, data({**chat_chunk({}), "choices": [], "usage": USAGE})),
    (0.0, "data: [DONE]\n\n"),
)

TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]


async def test_a_tool_call_only_turn_has_ttft_itl_and_tpot_in_every_client_flavour() -> None:
    requests = {
        "/v1/chat/completions": STREAM_BODY | {"tools": TOOLS},
        "/v1/messages": {
            "model": "ep_ep1",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "bash", "input_schema": {"type": "object"}}],
            "stream": True,
        },
        "/v1/responses": {
            "model": "ep_ep1",
            "input": "hi",
            "tools": [{"type": "function", "name": "bash", "parameters": {"type": "object"}}],
            "stream": True,
        },
    }
    for path, body in requests.items():
        state, clock = timed_state()
        add_route(state)
        secret = await issue(state)
        async with client_for(state, streaming(clock, TOOL_CALL_STEPS)) as client:
            response = await client.post(path, json=body, headers=auth(secret))
            assert response.status_code == 200, (path, response.text)
        (record,) = await state.usage_for("trial-ep1")
        assert timing_of(record) == EXPECTED, path
