"""Non-streaming Anthropic Messages <-> Chat Completions translation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalspleef.gateway.protocols import anthropic_request_to_chat, chat_response_to_anthropic

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_plain_text_exchange() -> None:
    fixture = _load("anthropic_plain_text.json")
    assert anthropic_request_to_chat(fixture["request"]) == fixture["expected_chat"]
    assert (
        chat_response_to_anthropic(fixture["response"], model="ep_test")
        == fixture["expected_anthropic"]
    )


def test_multi_block_system_prompt_is_joined_in_order() -> None:
    fixture = _load("anthropic_multi_block_system.json")
    assert anthropic_request_to_chat(fixture["request"]) == fixture["expected_chat"]


def test_tool_call_round_trip() -> None:
    fixture = _load("anthropic_tool_round_trip.json")
    assert anthropic_request_to_chat(fixture["request"]) == fixture["expected_chat"]
    assert (
        chat_response_to_anthropic(fixture["response"], model="ep_test")
        == fixture["expected_anthropic"]
    )


def test_parallel_tool_calls() -> None:
    fixture = _load("anthropic_parallel_tool_calls.json")
    assert anthropic_request_to_chat(fixture["request"]) == fixture["expected_chat"]
    assert (
        chat_response_to_anthropic(fixture["response"], model="ep_test")
        == fixture["expected_anthropic"]
    )


@pytest.mark.parametrize("case", _load("anthropic_finish_reasons.json")["cases"])
def test_finish_reason_mapping(case: dict[str, str | None]) -> None:
    response = {
        "id": "chatcmpl-reason",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "x"},
                "finish_reason": case["finish_reason"],
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    translated = chat_response_to_anthropic(response, model="ep_test")
    assert translated["stop_reason"] == case["stop_reason"]


def test_cached_tokens_map_to_anthropic_cache_read() -> None:
    response = {
        "id": "chatcmpl-cache",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "x"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    usage = chat_response_to_anthropic(response, model="ep_test")["usage"]
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 80,
    }


def test_tool_result_is_emitted_as_tool_message_before_user_text() -> None:
    request = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                    {"type": "text", "text": "continue"},
                ],
            }
        ],
    }
    chat = anthropic_request_to_chat(request)
    assert chat["messages"] == [
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]


def test_unsupported_content_block_raises() -> None:
    request = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {"role": "user", "content": [{"type": "image", "source": {}}]},
        ],
    }
    with pytest.raises(ValueError):
        anthropic_request_to_chat(request)
