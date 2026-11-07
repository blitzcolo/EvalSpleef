"""Translation between the Anthropic Messages protocol and OpenAI Chat Completions.

The gateway exposes ``/v1/messages`` and forwards every request upstream as
``chat_completions`` (TECH-DESIGN section 6.3). Everything here is a pure function or a
small state machine: no I/O, no framework imports, no clocks and no randomness.

Covered
-------
* ``system`` as a string or as a list of ``text`` blocks.
* Message blocks ``text``, ``tool_use`` and ``tool_result``.
* Tools, ``tool_choice`` (``auto`` / ``any`` / ``tool`` / ``none``), ``max_tokens``,
  ``stop_sequences``, ``temperature``, ``top_p``, ``stream``.
* Non-streaming responses with text and tool calls, usage and stop reasons.
* Streaming chunks with text deltas, (parallel) tool calls, usage and finish reasons.

Not covered
-----------
* Image, document and ``thinking`` blocks, ``top_k`` and ``metadata`` are not translated;
  unsupported content or tools raise :class:`ValueError` instead of being dropped.
* ``chat_response_to_anthropic`` expects a single choice and ignores ``logprobs``.
* Streaming tool calls are buffered per OpenAI tool index and emitted as complete
  ``tool_use`` blocks in ``finish()``: Anthropic cannot interleave content blocks, so a
  parallel tool call cannot be streamed argument-by-argument without blocking text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]

_STOP_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _map_finish_reason(finish_reason: str | None) -> str:
    if finish_reason is None:
        return "end_turn"
    return _STOP_REASON_MAP.get(finish_reason, "end_turn")


def _sse(event: str, payload: JsonObject) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _require_object(value: object, where: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object")
    return value


def _content_blocks(content: object, where: str) -> list[JsonObject]:
    """Normalise ``content`` into a list of block objects; a bare string is one text block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[JsonObject] = []
        for block in content:
            blocks.append(_require_object(block, where))
        return blocks
    raise ValueError(f"{where}: content must be a string or a list of blocks")


def _text_of(blocks: list[JsonObject], where: str) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.get("type") != "text":
            raise ValueError(f"{where}: expected only text blocks")
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{where}: text block without a string text")
        parts.append(text)
    return "\n".join(parts)


def _system_text(system: object) -> str:
    if isinstance(system, str):
        return system
    return _text_of(_content_blocks(system, "system"), "system")


def _tool_result_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return _text_of(_content_blocks(content, "tool_result"), "tool_result")


def _assistant_message(blocks: list[JsonObject], where: str) -> JsonObject:
    text_parts: list[str] = []
    tool_calls: list[JsonObject] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{where}: text block without a string text")
            text_parts.append(text)
        elif block_type == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"{where}: tool_use block without an id")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{where}: tool_use block without a name")
            tool_input = block.get("input", {})
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                    },
                }
            )
        else:
            raise ValueError(f"{where}: unsupported content block type {block_type!r}")

    message: JsonObject = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _user_messages(blocks: list[JsonObject], where: str) -> list[JsonObject]:
    """Tool results become ``tool`` messages first, so they directly follow the assistant call."""
    tool_messages: list[JsonObject] = []
    text_parts: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{where}: text block without a string text")
            text_parts.append(text)
        elif block_type == "tool_result":
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"{where}: tool_result block without a tool_use_id")
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_result_text(block.get("content")),
                }
            )
        else:
            raise ValueError(f"{where}: unsupported content block type {block_type!r}")

    messages = list(tool_messages)
    if text_parts:
        messages.append({"role": "user", "content": "\n".join(text_parts)})
    return messages


def _convert_tool(tool: object) -> JsonObject:
    spec = _require_object(tool, "tools")
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tools: every tool needs a non-empty name")
    function: JsonObject = {"name": name}
    description = spec.get("description")
    if isinstance(description, str):
        function["description"] = description
    input_schema = spec.get("input_schema")
    function["parameters"] = input_schema if isinstance(input_schema, dict) else {}
    return {"type": "function", "function": function}


def _convert_tool_choice(choice: object) -> object:
    if isinstance(choice, str):
        if choice in {"auto", "required", "none"}:
            return choice
        raise ValueError(f"tool_choice: unsupported value {choice!r}")
    spec = _require_object(choice, "tool_choice")
    kind = spec.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_choice: type tool needs a name")
        return {"type": "function", "function": {"name": name}}
    raise ValueError(f"tool_choice: unsupported type {kind!r}")


def anthropic_request_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request body into an OpenAI Chat Completions body."""
    messages: list[JsonObject] = []
    system = body.get("system")
    if system is not None:
        system_text = _system_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    raw_messages = body.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ValueError("messages must be a list")
    for position, raw in enumerate(raw_messages):
        where = f"messages[{position}]"
        message = _require_object(raw, where)
        role = message.get("role")
        blocks = _content_blocks(message.get("content", ""), where)
        if role == "assistant":
            messages.append(_assistant_message(blocks, where))
        elif role == "user":
            messages.extend(_user_messages(blocks, where))
        else:
            raise ValueError(f"{where}: unsupported role {role!r}")

    out: JsonObject = {"model": body.get("model", ""), "messages": messages}
    for field in ("max_tokens", "temperature", "top_p"):
        if body.get(field) is not None:
            out[field] = body[field]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if "stream" in body:
        out["stream"] = body["stream"]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = [_convert_tool(tool) for tool in tools]
    if body.get("tool_choice") is not None:
        out["tool_choice"] = _convert_tool_choice(body["tool_choice"])
    return out


def _usage_from_chat(usage: object) -> JsonObject:
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int):
            prompt_tokens = prompt
        completion = usage.get("completion_tokens")
        if isinstance(completion, int):
            completion_tokens = completion
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
            if isinstance(cached, int):
                cached_tokens = cached
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached_tokens,
    }


def _chat_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                raise ValueError("response content: unsupported content part")
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("response content: text part without a string text")
            parts.append(text)
        return "\n".join(parts)
    return ""


def _chat_tool_input(arguments: object) -> JsonObject:
    if arguments is None or arguments == "":
        return {}
    if not isinstance(arguments, str):
        raise ValueError("tool call arguments must be a JSON string")
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise ValueError("tool call arguments must decode to a JSON object")
    return parsed


def chat_response_to_anthropic(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Translate a non-streaming OpenAI Chat Completions response into an Anthropic message."""
    choices = resp.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        message = {}

    blocks: list[JsonObject] = []
    text = _chat_text(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})

    has_tool_use = False
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            spec = _require_object(call, "tool_calls")
            function = spec.get("function")
            function = function if isinstance(function, dict) else {}
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("tool call without a function name")
            call_id = spec.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("tool call without an id")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": _chat_tool_input(function.get("arguments")),
                }
            )
            has_tool_use = True

    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    if not isinstance(finish_reason, str):
        finish_reason = None
    stop_reason = _map_finish_reason(finish_reason)
    if has_tool_use and stop_reason == "end_turn":
        stop_reason = "tool_use"

    response_id = resp.get("id")
    if not isinstance(response_id, str) or not response_id:
        response_id = "msg_unknown"

    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage_from_chat(resp.get("usage")),
    }


@dataclass
class _ToolBuffer:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


class ChatToAnthropicStream:
    """Convert parsed OpenAI Chat Completions streaming chunks into Anthropic SSE strings.

    ``feed`` consumes one decoded chunk and returns the events it produced; ``finish``
    returns the closing events. Tool calls are buffered and emitted as complete
    ``tool_use`` blocks by ``finish``, because Anthropic content blocks cannot interleave.
    """

    def __init__(self, *, model: str) -> None:
        self._model = model
        self._started = False
        self._finished = False
        self._message_id = "msg_unknown"
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_creation_tokens = 0
        self._cache_read_tokens = 0
        self._next_index = 0
        self._text_index: int | None = None
        self._text_open = False
        self._finish_reason: str | None = None
        self._saw_tool_calls = False
        self._tool_buffers: dict[int, _ToolBuffer] = {}
        self._tool_order: list[int] = []

    def feed(self, chunk: dict[str, Any]) -> list[str]:
        if self._finished:
            raise RuntimeError("feed() called after finish()")
        self._update_usage(chunk)
        events = self._start()

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.extend(self._text_delta(content))
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for position, raw in enumerate(tool_calls):
                    if isinstance(raw, dict):
                        events.extend(self._tool_delta(raw, position))
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str):
            self._finish_reason = finish_reason
        return events

    def finish(self) -> list[str]:
        if self._finished:
            return []
        self._finished = True
        events = self._start()
        if self._text_open:
            self._text_open = False
            events.append(
                _sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_index},
                )
            )
        events.extend(self._flush_tools())
        stop_reason = _map_finish_reason(self._finish_reason)
        if self._saw_tool_calls and stop_reason == "end_turn":
            stop_reason = "tool_use"
        events.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": self._usage(),
                },
            )
        )
        events.append(_sse("message_stop", {"type": "message_stop"}))
        return events

    def _start(self) -> list[str]:
        if self._started:
            return []
        self._started = True
        return [_sse("message_start", {"type": "message_start", "message": self._message()})]

    def _message(self) -> JsonObject:
        return {
            "id": self._message_id,
            "type": "message",
            "role": "assistant",
            "model": self._model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": self._usage(),
        }

    def _usage(self) -> JsonObject:
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cache_creation_input_tokens": self._cache_creation_tokens,
            "cache_read_input_tokens": self._cache_read_tokens,
        }

    def _update_usage(self, chunk: JsonObject) -> None:
        response_id = chunk.get("id")
        if isinstance(response_id, str) and response_id:
            self._message_id = response_id
        usage = chunk.get("usage")
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int):
            self._input_tokens = prompt
        completion = usage.get("completion_tokens")
        if isinstance(completion, int):
            self._output_tokens = completion
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
            if isinstance(cached, int):
                self._cache_read_tokens = cached
            created = details.get("cache_creation_input_tokens")
            if isinstance(created, int):
                self._cache_creation_tokens = created

    def _text_delta(self, text: str) -> list[str]:
        events: list[str] = []
        if not self._text_open:
            events.extend(self._flush_tools())
            self._text_index = self._next_index
            self._next_index += 1
            self._text_open = True
            events.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        events.append(
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def _tool_delta(self, raw: JsonObject, position: int) -> list[str]:
        events: list[str] = []
        self._saw_tool_calls = True
        if self._text_open:
            self._text_open = False
            events.append(
                _sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_index},
                )
            )
        index = raw.get("index")
        tool_index = index if isinstance(index, int) else position
        buffer = self._tool_buffers.get(tool_index)
        if buffer is None:
            buffer = _ToolBuffer()
            self._tool_buffers[tool_index] = buffer
            self._tool_order.append(tool_index)
        call_id = raw.get("id")
        if isinstance(call_id, str) and call_id and not buffer.call_id:
            buffer.call_id = call_id
        function = raw.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name and not buffer.name:
                buffer.name = name
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                buffer.arguments += arguments
        return events

    def _flush_tools(self) -> list[str]:
        events: list[str] = []
        for tool_index in self._tool_order:
            buffer = self._tool_buffers.get(tool_index)
            if buffer is None:
                continue
            block_index = self._next_index
            self._next_index += 1
            call_id = buffer.call_id or f"toolu_{tool_index}"
            events.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call_id,
                            "name": buffer.name,
                            "input": {},
                        },
                    },
                )
            )
            if buffer.arguments:
                events.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": buffer.arguments,
                            },
                        },
                    )
                )
            events.append(
                _sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": block_index},
                )
            )
        self._tool_buffers.clear()
        self._tool_order.clear()
        return events
