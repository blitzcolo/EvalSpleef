"""Translation between the OpenAI Responses protocol and OpenAI Chat Completions.

The gateway exposes ``/v1/responses`` and forwards upstream requests as
``chat_completions`` (TECH-DESIGN section 6.3). This is the lower-priority protocol: the
documented core is translated and everything else fails loudly instead of being dropped.

Covered
-------
* ``instructions`` and ``input`` as a string or as items of type ``message``,
  ``function_call`` and ``function_call_output``.
* Text and assistant/tool messages; consecutive ``function_call`` items are merged into
  one assistant message with several tool calls, which is what Chat Completions expects.
* Function tools, ``tool_choice`` (``auto`` / ``required`` / ``none`` / a named function),
  ``max_output_tokens``, ``temperature``, ``top_p``, ``stream`` and
  ``parallel_tool_calls``.
* Non-streaming responses with text and function calls, usage and completion status.
* Streaming core events: ``response.created``, ``response.output_text.delta``,
  ``response.function_call_arguments.delta`` and ``response.completed``.

Not covered
-----------
* Built-in tools (``web_search``, ``file_search``, ``code_interpreter``, ``custom``),
  image/audio input parts, structured-output ``text.format``, ``reasoning``,
  ``previous_response_id`` and stateful conversation objects raise :class:`ValueError`.
* The finer-grained streaming events (``response.output_item.added``,
  ``response.content_part.added``, ``response.output_text.done``,
  ``response.function_call_arguments.done``) and ``response.incomplete`` are not emitted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]

_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
_CHAT_ROLES = frozenset({"system", "user", "assistant"})
_ROLE_MAP = {"developer": "system"}


def _sse(event: str, payload: JsonObject) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _require_object(value: object, where: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object")
    return value


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _responses_content(content: object, where: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError(f"{where}: content must be a string or a list of parts")
    parts: list[str] = []
    for part in content:
        spec = _require_object(part, where)
        if spec.get("type") not in _TEXT_PART_TYPES:
            raise ValueError(f"{where}: unsupported content part {spec.get('type')!r}")
        text = spec.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{where}: text part without a string text")
        parts.append(text)
    return "\n".join(parts)


def _output_text(output: object) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def _convert_message_item(item: JsonObject, where: str) -> JsonObject:
    role = item.get("role", "user")
    if not isinstance(role, str):
        raise ValueError(f"{where}: message role must be a string")
    role = _ROLE_MAP.get(role, role)
    if role not in _CHAT_ROLES:
        raise ValueError(f"{where}: unsupported message role {role!r}")
    return {"role": role, "content": _responses_content(item.get("content"), where)}


def _convert_function_calls(items: list[JsonObject], where: str) -> JsonObject:
    tool_calls: list[JsonObject] = []
    for item in items:
        call_id = item.get("call_id", item.get("id"))
        if not isinstance(call_id, str) or not call_id:
            raise ValueError(f"{where}: function_call without a call_id")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: function_call without a name")
        arguments = item.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def _convert_tool(tool: object) -> JsonObject:
    spec = _require_object(tool, "tools")
    if spec.get("type") != "function":
        raise ValueError(f"tools: unsupported tool type {spec.get('type')!r}")
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tools: every function tool needs a non-empty name")
    function: JsonObject = {"name": name}
    description = spec.get("description")
    if isinstance(description, str):
        function["description"] = description
    parameters = spec.get("parameters")
    function["parameters"] = parameters if isinstance(parameters, dict) else {}
    strict = spec.get("strict")
    if isinstance(strict, bool):
        function["strict"] = strict
    return {"type": "function", "function": function}


def _convert_tool_choice(choice: object) -> object:
    if isinstance(choice, str):
        if choice in {"auto", "required", "none"}:
            return choice
        raise ValueError(f"tool_choice: unsupported value {choice!r}")
    spec = _require_object(choice, "tool_choice")
    if spec.get("type") != "function":
        raise ValueError(f"tool_choice: unsupported type {spec.get('type')!r}")
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tool_choice: named function choice needs a name")
    return {"type": "function", "function": {"name": name}}


def _check_unsupported(body: JsonObject) -> None:
    if body.get("previous_response_id") is not None:
        raise ValueError("unsupported Responses field: previous_response_id")
    if body.get("reasoning") is not None:
        raise ValueError("unsupported Responses field: reasoning")
    text_spec = body.get("text")
    if isinstance(text_spec, dict):
        fmt = text_spec.get("format")
        if isinstance(fmt, dict) and fmt.get("type") not in (None, "text"):
            raise ValueError("unsupported Responses text format")


def responses_request_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI Responses request body into an OpenAI Chat Completions body."""
    _check_unsupported(body)
    messages: list[JsonObject] = []
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        if instructions:
            messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        pending_calls: list[JsonObject] = []
        for position, raw in enumerate(raw_input):
            where = f"input[{position}]"
            item = _require_object(raw, where)
            item_type = item.get("type")
            if item_type == "function_call":
                pending_calls.append(item)
                continue
            if pending_calls:
                messages.append(_convert_function_calls(pending_calls, where))
                pending_calls = []
            if item_type == "message":
                messages.append(_convert_message_item(item, where))
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(f"{where}: function_call_output without a call_id")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _output_text(item.get("output")),
                    }
                )
            else:
                raise ValueError(f"{where}: unsupported input item type {item_type!r}")
        if pending_calls:
            messages.append(_convert_function_calls(pending_calls, "input"))
    else:
        raise ValueError("input must be a string or a list of items")

    out: JsonObject = {"model": body.get("model", ""), "messages": messages}
    max_output_tokens = body.get("max_output_tokens")
    if max_output_tokens is not None:
        out["max_tokens"] = max_output_tokens
    for field in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if body.get(field) is not None:
            out[field] = body[field]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = [_convert_tool(tool) for tool in tools]
    if body.get("tool_choice") is not None:
        out["tool_choice"] = _convert_tool_choice(body["tool_choice"])
    return out


def _responses_usage(usage: object) -> JsonObject:
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    if isinstance(usage, dict):
        prompt_tokens = _int(usage.get("prompt_tokens"))
        completion_tokens = _int(usage.get("completion_tokens"))
        total_tokens = _int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = _int(prompt_details.get("cached_tokens"))
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning_tokens = _int(completion_details.get("reasoning_tokens"))
    return {
        "input_tokens": prompt_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": completion_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": total_tokens,
    }


def _response_status(finish_reason: object) -> tuple[str, JsonObject | None]:
    if finish_reason == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish_reason == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def _response_id(resp: JsonObject) -> str:
    response_id = resp.get("id")
    if isinstance(response_id, str) and response_id:
        return response_id
    return "resp_unknown"


def _text_output_item(response_id: str, text: str) -> JsonObject:
    return {
        "type": "message",
        "id": f"msg_{response_id}",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_call_output_item(call: JsonObject) -> JsonObject:
    function = call.get("function")
    function = function if isinstance(function, dict) else {}
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tool call without a function name")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("tool call without an id")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = "{}"
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def _chat_content_text(content: object) -> str:
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


def chat_response_to_responses(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Translate a non-streaming OpenAI Chat Completions response into a Responses object."""
    choices = resp.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        message = {}

    response_id = _response_id(resp)
    output: list[JsonObject] = []
    text = _chat_content_text(message.get("content"))
    if text:
        output.append(_text_output_item(response_id, text))

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            output.append(_function_call_output_item(_require_object(call, "tool_calls")))

    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    status, incomplete_details = _response_status(finish_reason)
    return {
        "id": response_id,
        "object": "response",
        "created_at": _int(resp.get("created")),
        "status": status,
        "model": model,
        "output": output,
        "usage": _responses_usage(resp.get("usage")),
        "error": None,
        "incomplete_details": incomplete_details,
    }


@dataclass
class _ToolBuffer:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    output_index: int = 0


class ChatToResponsesStream:
    """Convert parsed OpenAI Chat Completions streaming chunks into Responses SSE strings.

    Only the core events are emitted: ``response.created``, ``response.output_text.delta``,
    ``response.function_call_arguments.delta`` and ``response.completed``. The completed
    event carries the fully assembled response, so text and tool arguments reassembled
    from deltas equal the non-streaming translation.
    """

    def __init__(self, *, model: str) -> None:
        self._model = model
        self._started = False
        self._finished = False
        self._sequence = 0
        self._response_id = "resp_unknown"
        self._created_at = 0
        self._text = ""
        self._text_seen = False
        self._finish_reason: str | None = None
        self._tool_buffers: dict[int, _ToolBuffer] = {}
        self._tool_order: list[int] = []
        self._usage: JsonObject = _responses_usage(None)

    def feed(self, chunk: dict[str, Any]) -> list[str]:
        if self._finished:
            raise RuntimeError("feed() called after finish()")
        events = self._start(chunk)
        self._update(chunk)

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
                self._text += content
                if not self._text_seen:
                    self._text_seen = True
                events.append(
                    self._emit(
                        {
                            "type": "response.output_text.delta",
                            "item_id": f"msg_{self._response_id}",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": content,
                        }
                    )
                )
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
        events = self._start({})
        status, incomplete_details = _response_status(self._finish_reason)
        response = self._response_object(status=status, incomplete_details=incomplete_details)
        events.append(self._emit({"type": "response.completed", "response": response}))
        return events

    def _start(self, chunk: JsonObject) -> list[str]:
        if self._started:
            return []
        self._started = True
        response_id = chunk.get("id")
        if isinstance(response_id, str) and response_id:
            self._response_id = response_id
        self._created_at = _int(chunk.get("created"))
        return [
            self._emit(
                {
                    "type": "response.created",
                    "response": self._response_object(
                        status="in_progress", incomplete_details=None
                    ),
                }
            )
        ]

    def _emit(self, payload: JsonObject) -> str:
        payload["sequence_number"] = self._sequence
        self._sequence += 1
        return _sse(str(payload["type"]), payload)

    def _update(self, chunk: JsonObject) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = _responses_usage(usage)

    def _tool_delta(self, raw: JsonObject, position: int) -> list[str]:
        index = raw.get("index")
        tool_index = index if isinstance(index, int) else position
        buffer = self._tool_buffers.get(tool_index)
        if buffer is None:
            buffer = _ToolBuffer(output_index=(1 if self._text_seen else 0) + tool_index)
            self._tool_buffers[tool_index] = buffer
            self._tool_order.append(tool_index)
        call_id = raw.get("id")
        if isinstance(call_id, str) and call_id and not buffer.call_id:
            buffer.call_id = call_id
        events: list[str] = []
        function = raw.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name and not buffer.name:
                buffer.name = name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                buffer.arguments += arguments
                item_id = f"fc_{buffer.call_id or tool_index}"
                events.append(
                    self._emit(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item_id,
                            "output_index": buffer.output_index,
                            "delta": arguments,
                        }
                    )
                )
        return events

    def _response_object(self, *, status: str, incomplete_details: JsonObject | None) -> JsonObject:
        output: list[JsonObject] = []
        if self._text:
            output.append(_text_output_item(self._response_id, self._text))
        for tool_index in self._tool_order:
            buffer = self._tool_buffers.get(tool_index)
            if buffer is None:
                continue
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{buffer.call_id or tool_index}",
                    "call_id": buffer.call_id,
                    "name": buffer.name,
                    "arguments": buffer.arguments,
                    "status": "completed",
                }
            )
        return {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "model": self._model,
            "output": output,
            "usage": self._usage if status != "in_progress" else None,
            "error": None,
            "incomplete_details": incomplete_details,
        }
