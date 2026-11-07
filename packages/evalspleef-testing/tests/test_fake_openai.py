from __future__ import annotations

import json
import socket
from typing import Any

import httpx

from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_openai import FakeOpenAIServer, count_tokens

_REPLY = "fake reply"


def _sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def _port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _client(server: FakeOpenAIServer, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.base_url, timeout=10, **kwargs)


def _chat_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "fake-model",
        "messages": [{"role": "user", "content": "hello there world"}],
    }
    payload.update(overrides)
    return payload


async def test_stream_sends_a_final_usage_chunk_when_requested() -> None:
    async with FakeOpenAIServer() as server, _client(server) as client:
        response = await client.post(
            "/chat/completions",
            json=_chat_payload(stream=True, stream_options={"include_usage": True}),
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    finish = [event for event in events if event["choices"]]
    assert finish[-1]["choices"][0]["finish_reason"] == "stop"
    usage_chunks = [event for event in events if event["choices"] == []]
    assert len(usage_chunks) == 1
    assert events[-1] == usage_chunks[0]
    assert usage_chunks[0]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": count_tokens(_REPLY),
        "total_tokens": 3 + count_tokens(_REPLY),
    }


async def test_stream_omits_the_usage_chunk_without_stream_options() -> None:
    async with FakeOpenAIServer() as server, _client(server) as client:
        response = await client.post("/chat/completions", json=_chat_payload(stream=True))
    events = _sse_events(response.text)
    assert all(event.get("usage") is None for event in events)


async def test_parallel_tool_calls_are_served_as_parseable_calls() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "parameters": {
                    "type": "object",
                    "properties": {"zone": {"type": "string"}},
                    "required": ["zone"],
                },
            },
        },
    ]
    async with FakeOpenAIServer(parallel_tool_calls=True) as server, _client(server) as client:
        response = await client.post(
            "/chat/completions",
            json=_chat_payload(
                messages=[{"role": "user", "content": "weather and time please"}],
                tools=tools,
            ),
        )
    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    calls = choice["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["get_weather", "get_time"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "x"}
    assert json.loads(calls[1]["function"]["arguments"]) == {"zone": "x"}


async def test_context_limit_returns_400_context_length_exceeded() -> None:
    async with FakeOpenAIServer(context_limit_tokens=8) as server, _client(server) as client:
        within = await client.post(
            "/chat/completions",
            json=_chat_payload(messages=[{"role": "user", "content": "needle: alpha bravo"}]),
        )
        beyond = await client.post(
            "/chat/completions",
            json=_chat_payload(messages=[{"role": "user", "content": " ".join(["word"] * 12)}]),
        )
    assert within.status_code == 200
    assert "alpha" in within.json()["choices"][0]["message"]["content"]
    assert beyond.status_code == 400
    error = beyond.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"


async def test_injected_errors_apply_to_the_first_n_calls() -> None:
    server = FakeOpenAIServer()
    server.inject_error(429, count=2)
    server.inject_error(503, count=1)
    statuses: list[int] = []
    bodies: list[dict[str, Any]] = []
    async with server, _client(server) as client:
        for _ in range(4):
            response = await client.post("/chat/completions", json=_chat_payload())
            statuses.append(response.status_code)
            bodies.append(response.json())
    assert statuses == [429, 429, 503, 200]
    assert bodies[0]["error"]["code"] == "rate_limit_exceeded"
    assert bodies[2]["error"]["type"] == "server_error"


async def test_error_window_is_measured_with_the_injected_clock() -> None:
    clock = FakeClock(start=100.0)
    server = FakeOpenAIServer(clock=clock)
    server.inject_error_for(503, seconds=30)
    async with server, _client(server) as client:
        first = await client.post("/chat/completions", json=_chat_payload())
        clock.advance(29)
        still_failing = await client.post("/chat/completions", json=_chat_payload())
        clock.advance(1)
        recovered = await client.post("/chat/completions", json=_chat_payload())
    assert [first.status_code, still_failing.status_code, recovered.status_code] == [503, 503, 200]


def test_sync_context_manager_serves_requests_and_releases_the_port() -> None:
    with FakeOpenAIServer() as server:
        port = server.port
        with httpx.Client(base_url=server.base_url, timeout=10) as client:
            assert client.get("/models").status_code == 200
    assert _port_closed(port)


async def test_async_context_manager_serves_requests_and_releases_the_port() -> None:
    async with FakeOpenAIServer() as server, _client(server) as client:
        port = server.port
        assert (await client.get("/models")).status_code == 200
    assert _port_closed(port)


async def test_max_tokens_truncates_with_finish_reason_length() -> None:
    async with FakeOpenAIServer(long_output_words=10) as server, _client(server) as client:
        response = await client.post("/chat/completions", json=_chat_payload(max_tokens=3))
    data = response.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "length"
    assert len(choice["message"]["content"].split()) == 3
    assert data["usage"]["completion_tokens"] == 3


async def test_stop_sequence_truncates_the_completion() -> None:
    async with FakeOpenAIServer() as server, _client(server) as client:
        response = await client.post("/chat/completions", json=_chat_payload(stop="reply"))
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "fake "


async def test_json_schema_response_format_returns_schema_valid_json() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "count", "tags"],
        "additionalProperties": False,
    }
    async with FakeOpenAIServer() as server, _client(server) as client:
        response = await client.post(
            "/chat/completions",
            json=_chat_payload(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "reply", "schema": schema, "strict": True},
                }
            ),
        )
    content = json.loads(response.json()["choices"][0]["message"]["content"])
    assert content == {"name": "x", "count": 0, "tags": ["x"]}


async def test_seed_makes_the_output_deterministic() -> None:
    async with FakeOpenAIServer() as server, _client(server) as client:
        first = await client.post("/chat/completions", json=_chat_payload(seed=7))
        second = await client.post("/chat/completions", json=_chat_payload(seed=7))
        other = await client.post("/chat/completions", json=_chat_payload(seed=8))
    contents = [
        response.json()["choices"][0]["message"]["content"] for response in (first, second, other)
    ]
    assert contents[0] == contents[1]
    assert contents[0] != contents[2]


async def test_scripted_reply_is_served_exactly_and_requests_are_logged() -> None:
    server = FakeOpenAIServer(api_key="secret")
    server.queue_reply("exact answer", reasoning_content="because")
    async with server, _client(server, headers={"Authorization": "Bearer secret"}) as client:
        response = await client.post(
            "/chat/completions",
            json=_chat_payload(messages=[{"role": "user", "content": "hi"}]),
        )
        async with _client(server) as unauthenticated:
            unauthorized = await unauthenticated.post("/chat/completions", json=_chat_payload())
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "exact answer"
    assert message["reasoning_content"] == "because"
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"
    assert len(server.requests) == 2
    assert server.requests[0].path == "/v1/chat/completions"
    assert server.requests[0].body["messages"][0]["content"] == "hi"


async def test_reasoning_content_can_be_disabled_per_request() -> None:
    async with FakeOpenAIServer(reasoning_content=True) as server, _client(server) as client:
        enabled = await client.post("/chat/completions", json=_chat_payload())
        disabled = await client.post(
            "/chat/completions",
            json=_chat_payload(reasoning={"enabled": False}),
        )
    assert "reasoning_content" in enabled.json()["choices"][0]["message"]
    assert "reasoning_content" not in disabled.json()["choices"][0]["message"]


async def test_mismatching_identity_is_reported_from_configuration() -> None:
    server = FakeOpenAIServer(
        model="declared-model", reported_model="other-model", system_fingerprint="fp_probe"
    )
    async with server, _client(server) as client:
        models = await client.get("/models")
        completion = await client.post("/chat/completions", json=_chat_payload())
    assert models.json()["data"][0]["id"] == "declared-model"
    data = completion.json()
    assert data["model"] == "other-model"
    assert data["system_fingerprint"] == "fp_probe"
