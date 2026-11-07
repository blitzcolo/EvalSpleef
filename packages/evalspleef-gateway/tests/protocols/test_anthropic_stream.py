"""Streaming Anthropic Messages events from Chat Completions chunks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalspleef.gateway.protocols import ChatToAnthropicStream, chat_response_to_anthropic

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _parse_sse(events: list[str]) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for raw in events:
        assert raw.endswith("\n\n"), raw
        lines = raw[:-2].split("\n")
        assert len(lines) == 2, raw
        assert lines[0].startswith("event: "), raw
        assert lines[1].startswith("data: "), raw
        name = lines[0][len("event: ") :]
        data = json.loads(lines[1][len("data: ") :])
        assert data["type"] == name, raw
        parsed.append((name, data))
    return parsed


def _run_stream(fixture: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    stream = ChatToAnthropicStream(model=fixture["model"])
    events: list[str] = []
    for chunk in fixture["chunks"]:
        events.extend(stream.feed(chunk))
    events.extend(stream.finish())
    return _parse_sse(events)


def test_streaming_text_sequence_and_reassembly() -> None:
    fixture = _load("anthropic_stream_text.json")
    parsed = _run_stream(fixture)

    assert [name for name, _ in parsed] == fixture["expected_events"]

    message_start = parsed[0][1]["message"]
    assert message_start["id"] == "chatcmpl-stream"
    assert message_start["model"] == fixture["model"]
    assert message_start["content"] == []

    text = "".join(data["delta"]["text"] for name, data in parsed if name == "content_block_delta")
    expected = chat_response_to_anthropic(fixture["non_stream_response"], model=fixture["model"])
    expected_text = "".join(
        block["text"] for block in expected["content"] if block["type"] == "text"
    )
    assert text == fixture["expected_text"] == expected_text

    message_delta = next(data for name, data in parsed if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == fixture["expected_stop_reason"]
    assert message_delta["delta"]["stop_reason"] == expected["stop_reason"]
    assert message_delta["usage"]["input_tokens"] == 5
    assert message_delta["usage"]["output_tokens"] == 2


def test_streaming_tool_call_sequence_and_reassembly() -> None:
    fixture = _load("anthropic_stream_tool_calls.json")
    parsed = _run_stream(fixture)

    assert [name for name, _ in parsed] == fixture["expected_events"]

    blocks: dict[int, dict[str, Any]] = {}
    arguments: dict[int, str] = {}
    for name, data in parsed:
        if name == "content_block_start":
            assert data["index"] not in blocks
            blocks[data["index"]] = dict(data["content_block"])
            arguments[data["index"]] = ""
        elif name == "content_block_delta":
            assert blocks[data["index"]]["type"] == "tool_use"
            assert data["delta"]["type"] == "input_json_delta"
            arguments[data["index"]] += data["delta"]["partial_json"]

    tools = []
    for index in sorted(blocks):
        block = blocks[index]
        assert block["type"] == "tool_use"
        tools.append(
            {
                "id": block["id"],
                "name": block["name"],
                "input": json.loads(arguments[index]),
            }
        )
    assert tools == fixture["expected_tools"]

    expected = chat_response_to_anthropic(fixture["non_stream_response"], model=fixture["model"])
    expected_tools = [
        {"id": block["id"], "name": block["name"], "input": block["input"]}
        for block in expected["content"]
        if block["type"] == "tool_use"
    ]
    assert tools == expected_tools

    message_delta = next(data for name, data in parsed if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == fixture["expected_stop_reason"]
    assert message_delta["delta"]["stop_reason"] == expected["stop_reason"]


def test_text_then_tool_switches_blocks_and_closes_text() -> None:
    stream = ChatToAnthropicStream(model="ep_test")
    events = stream.feed({"id": "c1", "choices": [{"index": 0, "delta": {"content": "hi"}}]})
    events.extend(
        stream.feed(
            {
                "id": "c1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            }
        )
    )
    events.extend(stream.finish())
    parsed = _parse_sse(events)
    assert [name for name, _ in parsed] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert [data["index"] for name, data in parsed if name == "content_block_start"] == [0, 1]


def test_finish_is_idempotent_and_feed_after_finish_fails() -> None:
    stream = ChatToAnthropicStream(model="ep_test")
    stream.feed({"id": "c1", "choices": []})
    first = stream.finish()
    assert first
    assert stream.finish() == []
    with pytest.raises(RuntimeError):
        stream.feed({"choices": []})
