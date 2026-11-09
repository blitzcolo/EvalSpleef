"""Chat Completions <-> Anthropic Messages translation for an Anthropic upstream."""

from __future__ import annotations

from typing import Any

import pytest

from evalspleef.gateway.protocols import anthropic_response_to_chat, chat_request_to_anthropic


def _chat(**extra: Any) -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5-5",
        "messages": [{"role": "user", "content": "grade this"}],
        "max_tokens": 64,
        **extra,
    }


def test_system_and_developer_messages_move_to_the_top_level_system() -> None:
    request = chat_request_to_anthropic(
        _chat(
            messages=[
                {"role": "system", "content": "be strict"},
                {"role": "developer", "content": [{"type": "text", "text": "json only"}]},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ]
        )
    )
    assert request["system"] == "be strict\n\njson only"
    assert request["messages"] == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]


def test_max_completion_tokens_and_stop_are_mapped() -> None:
    body = _chat(stop="END", max_completion_tokens=99)
    del body["max_tokens"]
    request = chat_request_to_anthropic(body)
    assert request["max_tokens"] == 99
    assert request["stop_sequences"] == ["END"]
    assert chat_request_to_anthropic(_chat(stop=["a", "b"]))["stop_sequences"] == ["a", "b"]


def test_max_tokens_is_required() -> None:
    body = _chat()
    del body["max_tokens"]
    with pytest.raises(ValueError, match="max_tokens"):
        chat_request_to_anthropic(body)


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        ("5m", {"type": "ephemeral"}),
        ("1h", {"type": "ephemeral", "ttl": "1h"}),
        (None, None),
    ],
)
def test_cache_control_follows_the_requested_ttl(ttl: Any, expected: Any) -> None:
    request = chat_request_to_anthropic(_chat(), cache_ttl=ttl)
    assert request.get("cache_control") == expected


def test_native_keys_and_sampling_are_forwarded_untouched() -> None:
    request = chat_request_to_anthropic(
        _chat(
            temperature=0.0,
            top_p=0.9,
            output_config={"effort": "high"},
            thinking={"type": "adaptive"},
        )
    )
    assert request["temperature"] == 0.0
    assert request["top_p"] == 0.9
    assert request["output_config"] == {"effort": "high"}
    assert request["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    "field",
    [
        "tools",
        "tool_choice",
        "logprobs",
        "logit_bias",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "user",
    ],
)
def test_fields_without_an_anthropic_equivalent_are_rejected_by_name(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        chat_request_to_anthropic(_chat(**{field: [1] if field == "tools" else True}))


def test_stream_flag_asks_for_a_streamed_reply_and_stream_options_are_consumed() -> None:
    request = chat_request_to_anthropic(_chat(stream=True, stream_options={"include_usage": True}))
    assert request["stream"] is True
    assert "stream_options" not in request
    assert "stream" not in chat_request_to_anthropic(_chat(stream=False))


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "integer"}},
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}


def _json_schema_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "answer", "strict": True, "schema": SCHEMA},
    }


def test_json_schema_response_format_becomes_output_config_format() -> None:
    request = chat_request_to_anthropic(_chat(response_format=_json_schema_format()))
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert request["output_config"]["format"]["schema"] is SCHEMA
    assert "response_format" not in request


def test_json_schema_is_merged_into_existing_output_config() -> None:
    client = {"effort": "high", "other": 1}
    request = chat_request_to_anthropic(
        _chat(output_config=client, response_format=_json_schema_format())
    )
    assert request["output_config"] == {
        "effort": "high",
        "other": 1,
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert client == {"effort": "high", "other": 1}


@pytest.mark.parametrize(
    "response_format",
    [{"type": "json_object"}, {"type": "text"}, {"type": "json_schema"}],
)
def test_other_response_formats_are_rejected_by_name(response_format: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="response_format"):
        chat_request_to_anthropic(_chat(response_format=response_format))


def test_response_format_cannot_override_an_output_config_format() -> None:
    with pytest.raises(ValueError, match=r"output_config\.format"):
        chat_request_to_anthropic(
            _chat(output_config={"format": {}}, response_format=_json_schema_format())
        )


def test_n_other_than_one_is_rejected_but_one_is_fine() -> None:
    with pytest.raises(ValueError, match=r"^n is"):
        chat_request_to_anthropic(_chat(n=2))
    assert chat_request_to_anthropic(_chat(n=1))["messages"]


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "x", "tool_call_id": "t"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]},
    ],
)
def test_tool_and_non_text_messages_are_rejected(message: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="not supported"):
        chat_request_to_anthropic(_chat(messages=[message]))


def _anthropic(**overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 3750,
            "cache_read_input_tokens": 0,
            "output_tokens": 200,
            "output_tokens_details": {"thinking_tokens": 150},
        },
    }
    response.update(overrides)
    return response


def test_response_joins_text_blocks_and_ignores_thinking() -> None:
    chat = anthropic_response_to_chat(_anthropic())
    assert chat["id"] == "msg_1"
    assert chat["model"] == "claude-sonnet-5-5"
    assert chat["choices"][0]["message"] == {"role": "assistant", "content": "Hello world"}


@pytest.mark.parametrize(
    ("stop_reason", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
    ],
)
def test_response_finish_reasons(stop_reason: str, finish: str) -> None:
    chat = anthropic_response_to_chat(_anthropic(stop_reason=stop_reason))
    assert chat["choices"][0]["finish_reason"] == finish


def test_usage_folds_cache_tokens_into_the_prompt_count() -> None:
    usage = anthropic_response_to_chat(_anthropic())["usage"]
    assert usage["prompt_tokens"] == 10 + 3750
    assert usage["completion_tokens"] == 200
    assert usage["total_tokens"] == 3960
    assert usage["prompt_tokens_details"] == {"cached_tokens": 0}
    assert usage["cache_creation_input_tokens"] == 3750
    assert usage["completion_tokens_details"] == {"reasoning_tokens": 150}

    read = _anthropic(
        usage={"input_tokens": 10, "cache_read_input_tokens": 3750, "output_tokens": 5}
    )
    usage = anthropic_response_to_chat(read)["usage"]
    assert usage["prompt_tokens"] == 3760
    assert usage["prompt_tokens_details"] == {"cached_tokens": 3750}
    assert usage["cache_creation_input_tokens"] == 0
    assert "completion_tokens_details" not in usage
