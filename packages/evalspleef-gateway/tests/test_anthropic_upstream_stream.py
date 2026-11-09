"""Streaming and structured output through an Anthropic Messages upstream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from evalspleef.gateway.state import EVENT_ENDPOINT_MISMATCH, GatewayState
from evalspleef.spi.gateway import COST_HEADER

from .conftest import auth
from .test_anthropic_upstream import (
    CHAT,
    SONNET_PRICE,
    anthropic_reply,
    anthropic_route,
    post,
    recording,
)
from .test_data_app import add_route, client_for, issue

STREAM_CHAT = CHAT | {"stream": True, "stream_options": {"include_usage": True}}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "integer"}},
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}


def sse(*events: dict[str, Any]) -> bytes:
    """Anthropic wire format: an ``event:`` line and a ``data:`` line per event."""
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_start(usage: dict[str, Any], model: str = "claude-sonnet-5-5") -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "model": model,
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "usage": {"output_tokens": 8} | usage,
        },
    }


def delta(index: int, kind: str, **fields: str) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": kind, **fields},
    }


def message_delta(usage: dict[str, Any], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    }


PING = {"type": "ping"}
STOP = {"type": "message_stop"}

# Shapes copied from live streams: a cache write, then a cache read with thinking.
WRITE_STREAM = sse(
    message_start({"input_tokens": 4, "cache_creation_input_tokens": 3745}),
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    PING,
    delta(0, "text_delta", text="1, 2, 3,"),
    delta(0, "text_delta", text=" 4, 5"),
    {"type": "content_block_stop", "index": 0},
    message_delta(
        {
            "input_tokens": 4,
            "cache_creation_input_tokens": 3745,
            "cache_read_input_tokens": 0,
            "output_tokens": 51,
            "output_tokens_details": {"thinking_tokens": 0},
        }
    ),
    STOP,
)
READ_STREAM = sse(
    message_start({"input_tokens": 4, "cache_read_input_tokens": 3745}),
    PING,
    delta(0, "thinking_delta", thinking=""),
    delta(0, "thinking_delta", thinking="let me count"),
    delta(0, "signature_delta", signature="c2ln"),
    delta(1, "text_delta", text="1, 2"),
    message_delta(
        {
            "input_tokens": 4,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 3745,
            "output_tokens": 182,
            "output_tokens_details": {"thinking_tokens": 121},
        }
    ),
    STOP,
)


async def stream_chat(
    state: GatewayState, body: bytes, request_body: dict[str, Any] | None = None
) -> tuple[httpx.Response, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    handler = recording(seen, lambda _: httpx.Response(200, content=body))
    return await post(state, handler, request_body or STREAM_CHAT), seen


def chunks_of(response: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
    ]


async def test_text_stream_becomes_chat_chunks_with_a_usage_chunk(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route())
    state.set_price("ep_ep1", SONNET_PRICE)
    response, seen = await stream_chat(state, WRITE_STREAM)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = chunks_of(response)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "1, 2, 3, 4, 5"
    finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert [c["choices"][0]["finish_reason"] for c in finals] == ["stop"]
    usage_chunk = chunks[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["prompt_tokens"] == 4 + 3745
    assert usage_chunk["usage"]["completion_tokens"] == 51
    assert usage_chunk["usage"]["cache_creation_input_tokens"] == 3745
    assert response.text.rstrip().endswith("data: [DONE]")

    (request,) = seen
    assert str(request.url) == "http://upstream.test/v1/messages"
    sent = json.loads(request.content)
    assert sent["stream"] is True
    assert "stream_options" not in sent
    assert sent["cache_control"] == {"type": "ephemeral"}

    (record,) = await state.usage_for("trial-ep1")
    assert (record.input_tokens, record.cached_tokens, record.output_tokens) == (3749, 0, 51)
    # (4 * 2.00 + 3745 * 2.50 + 51 * 10.00) / 1e6
    assert record.cost_usd == pytest.approx(0.0098805)


async def test_streamed_cache_writes_are_charged_at_the_write_price(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    state.set_price("ep_ep1", SONNET_PRICE)
    await stream_chat(state, WRITE_STREAM, CHAT | {"stream": True})
    (record,) = await state.usage_for("trial-ep1")
    # (4 * 2.00 + 3745 * 2.50 + 51 * 10.00) / 1e6; priced as input it would be 0.008008
    assert record.cost_usd == pytest.approx(0.0098805)


async def test_thinking_goes_to_reasoning_content_and_cache_reads_are_priced(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    state.set_price("ep_ep1", SONNET_PRICE)
    response, _ = await stream_chat(state, READ_STREAM)

    deltas = [c["choices"][0]["delta"] for c in chunks_of(response) if c["choices"]]
    assert [d["reasoning_content"] for d in deltas if "reasoning_content" in d] == ["let me count"]
    assert [d["content"] for d in deltas if d.get("content")] == ["1, 2"]
    assert all("signature" not in json.dumps(d) for d in deltas)

    (record,) = await state.usage_for("trial-ep1")
    assert (record.input_tokens, record.cached_tokens, record.output_tokens) == (3749, 3745, 182)
    assert record.reasoning_tokens == 121
    # (4 * 2.00 + 3745 * 0.10 + 182 * 10.00) / 1e6
    assert record.cost_usd == pytest.approx(0.0022025)


async def test_usage_chunk_is_sent_only_when_the_client_asked_for_it(state: GatewayState) -> None:
    add_route(state, overrides=anthropic_route())
    body = CHAT | {"stream": True}
    response, _ = await stream_chat(state, WRITE_STREAM, body)
    assert all("usage" not in chunk for chunk in chunks_of(response))
    # Accounting does not depend on the client's stream_options.
    (record,) = await state.usage_for("trial-ep1")
    assert record.output_tokens == 51


async def test_usage_falls_back_to_message_start_without_a_message_delta_usage(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    events = sse(
        message_start({"input_tokens": 4, "cache_read_input_tokens": 100}),
        delta(0, "text_delta", text="x"),
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        STOP,
    )
    response, _ = await stream_chat(state, events)
    assert chunks_of(response)[-1]["usage"]["prompt_tokens"] == 104
    (record,) = await state.usage_for("trial-ep1")
    assert (record.input_tokens, record.cached_tokens) == (104, 100)


@pytest.mark.parametrize(
    ("stop_reason", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
    ],
)
async def test_stream_finish_reasons(state: GatewayState, stop_reason: str, finish: str) -> None:
    add_route(state, overrides=anthropic_route())
    events = sse(
        message_start({"input_tokens": 1}),
        delta(0, "text_delta", text="x"),
        message_delta({"output_tokens": 2}, stop_reason),
        STOP,
    )
    response, _ = await stream_chat(state, events)
    reasons = [
        c["choices"][0]["finish_reason"]
        for c in chunks_of(response)
        if c["choices"] and c["choices"][0]["finish_reason"]
    ]
    assert reasons == [finish]


async def test_an_error_event_ends_the_stream_with_an_error_and_a_502_record(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    events = sse(
        message_start({"input_tokens": 4}),
        delta(0, "text_delta", text="par"),
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    response, _ = await stream_chat(state, events)
    last = chunks_of(response)[-1]
    assert "Overloaded" in last["error"]["message"]
    assert last["error"]["code"] == "upstream_error"
    assert "data: [DONE]" not in response.text
    (record,) = await state.usage_for("trial-ep1")
    assert (record.status_code, record.error_code) == (502, "upstream_error")


class _BreaksMidStream(httpx.AsyncByteStream):
    def __init__(self, failure: httpx.TransportError) -> None:
        self._failure = failure

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse(message_start({"input_tokens": 4}), delta(0, "text_delta", text="par"))
        raise self._failure


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (httpx.ReadTimeout("slow upstream"), 504, "upstream_timeout"),
        (httpx.RemoteProtocolError("peer closed"), 502, "upstream_error"),
    ],
)
async def test_a_dropped_connection_ends_with_an_error_event(
    state: GatewayState, failure: httpx.TransportError, status: int, code: str
) -> None:
    add_route(state, overrides=anthropic_route())
    handler = recording([], lambda _: httpx.Response(200, stream=_BreaksMidStream(failure)))
    response = await post(state, handler, STREAM_CHAT)
    assert chunks_of(response)[-1]["error"]["code"] == code
    assert "data: [DONE]" not in response.text
    (record,) = await state.usage_for("trial-ep1")
    assert (record.status_code, record.error_code) == (status, code)


async def test_provider_lock_inspects_the_message_start_model(state: GatewayState) -> None:
    add_route(
        state,
        overrides=anthropic_route()
        | {"provider_lock": {"response_field": "model", "pattern": "^claude-"}},
    )
    events = sse(message_start({"input_tokens": 1}, model="other-vendor"), STOP)
    response, _ = await stream_chat(state, events)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "endpoint_mismatch"
    (event,) = [e for e in state.events() if e["type"] == EVENT_ENDPOINT_MISMATCH]
    assert event["got"] == "other-vendor"

    good = await stream_chat(state, WRITE_STREAM)
    assert good[0].status_code == 200


async def test_anthropic_clients_get_anthropic_events_from_an_anthropic_upstream(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route())
    secret = await issue(state)
    handler = recording([], lambda _: httpx.Response(200, content=WRITE_STREAM))
    body = {
        "model": "ep_ep1",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with client_for(state, handler) as client:
        response = await client.post("/v1/messages", json=body, headers=auth(secret))
    assert "event: message_start" in response.text
    assert "event: message_stop" in response.text
    assert "1, 2, 3," in response.text


async def test_json_schema_response_format_reaches_the_upstream_and_the_reply_validates(
    state: GatewayState,
) -> None:
    add_route(state, overrides=anthropic_route(output_config={"effort": "high"}))
    reply = anthropic_reply()
    reply["content"] = [{"type": "text", "text": '{"answer":"Paris","confidence":10}'}]
    seen: list[httpx.Request] = []
    response = await post(
        state,
        recording(seen, lambda _: httpx.Response(200, json=reply)),
        CHAT
        | {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": SCHEMA},
            }
        },
    )
    assert response.status_code == 200
    sent = json.loads(seen[0].content)
    assert sent["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert "response_format" not in sent
    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"answer": "Paris", "confidence": 10}
    assert COST_HEADER in response.headers


@pytest.mark.parametrize("response_format", [{"type": "json_object"}, {"type": "text"}])
async def test_other_response_formats_are_a_400_before_the_upstream(
    state: GatewayState, response_format: dict[str, Any]
) -> None:
    add_route(state, overrides=anthropic_route())
    seen: list[httpx.Request] = []
    response = await post(
        state,
        recording(seen, lambda _: httpx.Response(200, json=anthropic_reply())),
        CHAT | {"response_format": response_format},
    )
    assert response.status_code == 400
    assert "response_format" in response.json()["error"]["message"]
    assert seen == []
