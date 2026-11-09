"""Which streamed chunks count as generated text for TTFT, ITL and TPOT (client side)."""

from __future__ import annotations

from typing import Any

import pytest

from evalspleef.control.probes.client import carries_generated_text

# The generated-text rule, pinned with the same cases in the gateway and the control package
# (the gateway cannot import the control-side classifier): content, reasoning, or a tool call
# with a name or arguments counts; role-only, empty, finish-only and usage-only chunks do not.
GENERATED_TEXT_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("content", {"choices": [{"index": 0, "delta": {"content": "Hi"}}]}, True),
    ("empty content", {"choices": [{"index": 0, "delta": {"content": ""}}]}, False),
    ("null content", {"choices": [{"index": 0, "delta": {"content": None}}]}, False),
    ("reasoning", {"choices": [{"index": 0, "delta": {"reasoning_content": "hmm"}}]}, True),
    ("role only", {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}, False),
    (
        "role and empty content",
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        False,
    ),
    ("empty delta", {"choices": [{"index": 0, "delta": {}}]}, False),
    ("finish only", {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}, False),
    ("usage only", {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, False),
    ("no choices", {"id": "x"}, False),
    (
        "tool call name",
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "bash", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        True,
    ),
    (
        "tool call arguments",
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd"'}}]},
                }
            ]
        },
        True,
    ),
    (
        "tool call empty function",
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"name": "", "arguments": ""}}]
                    },
                }
            ]
        },
        False,
    ),
    (
        "tool call without function",
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1"}]}}]},
        False,
    ),
    ("empty tool calls", {"choices": [{"index": 0, "delta": {"tool_calls": []}}]}, False),
    (
        "second choice",
        {"choices": [{"index": 0, "delta": {}}, {"index": 1, "delta": {"content": "x"}}]},
        True,
    ),
]


@pytest.mark.parametrize(("name", "chunk", "expected"), GENERATED_TEXT_CASES)
def test_generated_text_rule(name: str, chunk: dict[str, Any], expected: bool) -> None:
    assert carries_generated_text(chunk) is expected, name
