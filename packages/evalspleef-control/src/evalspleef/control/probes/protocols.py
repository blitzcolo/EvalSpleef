"""P-13: gateway protocol conversion (Anthropic Messages and OpenAI Responses).

Each converted protocol runs the single-tool-call script (P-03) and the three-round tool
history script (P-05) through the gateway's own conversion layer.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from evalspleef.spi.probe import ProbeCheck

from .client import MESSAGES_PATH, RESPONSES_PATH, GatewayClient, as_dict, as_list
from .fixtures import (
    LOOKUP_DESCRIPTION,
    LOOKUP_PARAMS,
    LOOKUP_ROUNDS,
    LOOKUP_START_PROMPT,
    LOOKUP_SUMMARY_PROMPT,
    WEATHER_DESCRIPTION,
    WEATHER_PARAMS,
    lookup_arguments,
    lookup_call_id,
    lookup_result,
    valid_arguments,
    weather_prompt,
)

__all__ = ["probe_protocols"]

ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_HEADERS = {"anthropic-version": ANTHROPIC_VERSION}

_Script = Callable[[GatewayClient, str, float], Awaitable[tuple[bool, str]]]


async def _anthropic_single(
    client: GatewayClient, model: str, timeout_s: float
) -> tuple[bool, str]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": weather_prompt("Paris")}],
        "tools": [
            {
                "name": "get_weather",
                "description": WEATHER_DESCRIPTION,
                "input_schema": WEATHER_PARAMS,
            }
        ],
        "tool_choice": {"type": "auto"},
    }
    reply = await client.post(MESSAGES_PATH, body, timeout_s=timeout_s, headers=_ANTHROPIC_HEADERS)
    if not reply.ok:
        return False, reply.error
    for block in as_list(reply.body.get("content")):
        spec = as_dict(block)
        if spec.get("type") == "tool_use" and valid_arguments(spec.get("input"), "city"):
            return True, "tool_use block with a valid input"
    return False, "no tool_use block with a city input"


async def _anthropic_multi(client: GatewayClient, model: str, timeout_s: float) -> tuple[bool, str]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": LOOKUP_START_PROMPT}]
    for index in range(LOOKUP_ROUNDS):
        call_id = lookup_call_id(index)
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "lookup",
                        "input": lookup_arguments(index),
                    }
                ],
            }
        )
        results: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": call_id, "content": lookup_result(index)}
        ]
        if index == LOOKUP_ROUNDS - 1:
            results.append({"type": "text", "text": LOOKUP_SUMMARY_PROMPT})
        messages.append({"role": "user", "content": results})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 256,
        "messages": messages,
        "tools": [
            {
                "name": "lookup",
                "description": LOOKUP_DESCRIPTION,
                "input_schema": LOOKUP_PARAMS,
            }
        ],
        "tool_choice": {"type": "none"},
    }
    reply = await client.post(MESSAGES_PATH, body, timeout_s=timeout_s, headers=_ANTHROPIC_HEADERS)
    if not reply.ok:
        return False, reply.error
    blocks = [as_dict(block) for block in as_list(reply.body.get("content"))]
    has_text = any(block.get("type") == "text" and block.get("text") for block in blocks)
    has_tool = any(block.get("type") == "tool_use" for block in blocks)
    if has_text and not has_tool:
        return True, "text answer after three tool rounds"
    return False, "no plain text answer after the tool history"


async def _responses_single(
    client: GatewayClient, model: str, timeout_s: float
) -> tuple[bool, str]:
    body: dict[str, Any] = {
        "model": model,
        "max_output_tokens": 256,
        "input": weather_prompt("Paris"),
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": WEATHER_DESCRIPTION,
                "parameters": WEATHER_PARAMS,
            }
        ],
        "tool_choice": "auto",
    }
    reply = await client.post(RESPONSES_PATH, body, timeout_s=timeout_s)
    if not reply.ok:
        return False, reply.error
    for item in as_list(reply.body.get("output")):
        spec = as_dict(item)
        if spec.get("type") == "function_call" and valid_arguments(spec.get("arguments"), "city"):
            return True, "function_call item with valid arguments"
    return False, "no function_call item with a city argument"


async def _responses_multi(client: GatewayClient, model: str, timeout_s: float) -> tuple[bool, str]:
    items: list[dict[str, Any]] = [
        {"type": "message", "role": "user", "content": LOOKUP_START_PROMPT}
    ]
    for index in range(LOOKUP_ROUNDS):
        call_id = lookup_call_id(index)
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "lookup",
                "arguments": json.dumps(lookup_arguments(index)),
            }
        )
        items.append(
            {"type": "function_call_output", "call_id": call_id, "output": lookup_result(index)}
        )
    items.append({"type": "message", "role": "user", "content": LOOKUP_SUMMARY_PROMPT})
    body: dict[str, Any] = {
        "model": model,
        "max_output_tokens": 256,
        "input": items,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": LOOKUP_DESCRIPTION,
                "parameters": LOOKUP_PARAMS,
            }
        ],
        "tool_choice": "none",
    }
    reply = await client.post(RESPONSES_PATH, body, timeout_s=timeout_s)
    if not reply.ok:
        return False, reply.error
    output = [as_dict(item) for item in as_list(reply.body.get("output"))]
    has_text = any(
        part.get("type") == "output_text" and part.get("text")
        for item in output
        if item.get("type") == "message"
        for part in (as_dict(raw) for raw in as_list(item.get("content")))
    )
    has_call = any(item.get("type") == "function_call" for item in output)
    if has_text and not has_call:
        return True, "text answer after three tool rounds"
    return False, "no plain text answer after the tool history"


_PROTOCOLS: tuple[tuple[str, _Script, _Script], ...] = (
    ("anthropic_messages", _anthropic_single, _anthropic_multi),
    ("responses", _responses_single, _responses_multi),
)


async def probe_protocols(client: GatewayClient, model: str, *, timeout_s: float) -> ProbeCheck:
    """Run both converted protocols; ``data['protocols']`` lists those that fully pass."""
    working: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    for name, single, multi in _PROTOCOLS:
        single_ok, single_detail = await single(client, model, timeout_s)
        multi_ok, multi_detail = await multi(client, model, timeout_s)
        details[name] = {
            "tool_call": single_ok,
            "multi_turn_tools": multi_ok,
            "detail": f"single: {single_detail}; multi: {multi_detail}",
        }
        if single_ok and multi_ok:
            working.append(name)
    summary = ", ".join(
        f"{name}={'ok' if name in working else 'failed'}" for name, _, _ in _PROTOCOLS
    )
    return ProbeCheck(
        "P-13",
        len(working) == len(_PROTOCOLS),
        summary,
        {"protocols": working, "details": details},
    )
