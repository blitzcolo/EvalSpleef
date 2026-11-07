"""OpenAI Responses <-> Chat Completions translation, non-streaming and streaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalspleef.gateway.protocols import (
    ChatToResponsesStream,
    chat_response_to_responses,
    responses_request_to_chat,
)

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


def test_plain_text_exchange() -> None:
    fixture = _load("responses_plain_text.json")
    assert responses_request_to_chat(fixture["request"]) == fixture["expected_chat"]
    assert (
        chat_response_to_responses(fixture["response"], model="ep_test")
        == fixture["expected_responses"]
    )


def test_tool_round_trip() -> None:
    fixture = _load("responses_tool_round_trip.json")
    assert responses_request_to_chat(fixture["request"]) == fixture["expected_chat"]
    assert (
        chat_response_to_responses(fixture["response"], model="ep_test")
        == fixture["expected_responses"]
    )


def test_length_finish_reason_marks_response_incomplete() -> None:
    response = {
        "id": "chatcmpl-long",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "partial"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    translated = chat_response_to_responses(response, model="ep_test")
    assert translated["status"] == "incomplete"
    assert translated["incomplete_details"] == {"reason": "max_output_tokens"}


def test_builtin_tool_is_rejected() -> None:
    request = {
        "model": "gpt-5",
        "input": "hi",
        "tools": [{"type": "web_search"}],
    }
    with pytest.raises(ValueError):
        responses_request_to_chat(request)


def test_unsupported_input_item_is_rejected() -> None:
    request = {
        "model": "gpt-5",
        "input": [{"type": "reasoning", "summary": []}],
    }
    with pytest.raises(ValueError):
        responses_request_to_chat(request)


def test_streaming_events_reassemble_to_non_streaming_response() -> None:
    fixture = _load("responses_stream.json")
    stream = ChatToResponsesStream(model=fixture["model"])
    events: list[str] = []
    for chunk in fixture["chunks"]:
        events.extend(stream.feed(chunk))
    events.extend(stream.finish())
    parsed = _parse_sse(events)

    assert [name for name, _ in parsed] == fixture["expected_events"]
    numbers = [data["sequence_number"] for _, data in parsed]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)

    text = "".join(data["delta"] for name, data in parsed if name == "response.output_text.delta")
    arguments = "".join(
        data["delta"] for name, data in parsed if name == "response.function_call_arguments.delta"
    )
    assert text == fixture["expected_text"]
    assert arguments == fixture["expected_arguments"]

    completed = next(data for name, data in parsed if name == "response.completed")
    expected = chat_response_to_responses(fixture["non_stream_response"], model=fixture["model"])
    assert completed["response"]["status"] == fixture["expected_status"]
    assert completed["response"]["status"] == expected["status"]
    assert completed["response"]["usage"] == expected["usage"]
    assert completed["response"]["output"] == expected["output"]
