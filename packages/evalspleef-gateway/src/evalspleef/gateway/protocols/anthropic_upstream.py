"""Chat Completions <-> Anthropic Messages translation for an Anthropic *upstream*.

``anthropic.py`` serves Anthropic clients on top of a chat upstream; this module goes the
other way so a route with ``protocol: anthropic_messages`` can be called by any chat
client. Only plain text exchanges (streamed or not) and JSON-schema structured output are translated. A field without a faithful Anthropic
equivalent raises ``ValueError`` naming it instead of being dropped, so a caller never
believes it got tool calling, JSON mode or a seed that the upstream did not honour.
"""

from __future__ import annotations

from typing import Any, Literal

CacheTtl = Literal["5m", "1h"]

#: Chat keys that map onto Messages fields or are passed through untouched.
_ALLOWED_KEYS = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "temperature",
        "top_p",
        # Anthropic-native keys a route's default_params may supply.
        "output_config",
        "thinking",
        "response_format",
        # Consumed by the gateway: it only decides whether the final usage chunk is sent.
        "stream_options",
    }
)
_PASS_THROUGH_KEYS = ("temperature", "top_p", "output_config", "thinking")

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def _finish_reason(stop_reason: Any) -> str:
    return _FINISH_REASONS.get(stop_reason, "stop") if isinstance(stop_reason, str) else "stop"


def _unsupported(field: str) -> ValueError:
    return ValueError(f"{field} is not supported for anthropic_messages upstreams")


def _text_of(content: Any, where: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                kind = part.get("type") if isinstance(part, dict) else type(part).__name__
                raise _unsupported(f"{where} content part of type {kind!r}")
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{where} text part has no text")
            parts.append(text)
        return "".join(parts)
    raise ValueError(f"{where} must be a string or a list of text parts")


def chat_request_to_anthropic(
    body: dict[str, Any], *, cache_ttl: CacheTtl | None = "5m"
) -> dict[str, Any]:
    """Chat Completions request -> Anthropic Messages request.

    ``cache_ttl`` ``None`` leaves prompt caching off; otherwise the top-level
    ``cache_control`` breakpoint caches the whole prompt (``"1h"`` asks for the 1-hour TTL).
    """
    if body.get("n") not in (None, 1):
        raise _unsupported("n")
    for key in body:
        if key not in _ALLOWED_KEYS and key not in ("stream", "n"):
            raise _unsupported(key)
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        raise ValueError("max_tokens is required for anthropic_messages upstreams")

    system: list[str] = []
    messages: list[dict[str, Any]] = []
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("messages must be a list")
    for index, message in enumerate(raw_messages):
        where = f"messages[{index}]"
        if not isinstance(message, dict):
            raise ValueError(f"{where} must be an object")
        role = message.get("role")
        for key in message:
            if key not in ("role", "content"):
                raise _unsupported(f"{where}.{key}")
        text = _text_of(message.get("content"), where)
        if role in ("system", "developer"):
            system.append(text)
        elif role in ("user", "assistant"):
            messages.append({"role": role, "content": text})
        else:
            raise _unsupported(f"{where} role {role!r}")

    request: dict[str, Any] = {
        "model": body.get("model"),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        request["system"] = "\n\n".join(system)
    stop = body.get("stop")
    if stop is not None:
        request["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    for key in _PASS_THROUGH_KEYS:
        if key in body and body[key] is not None:
            request[key] = body[key]
    schema = _json_schema_of(body.get("response_format"))
    if schema is not None:
        # Merge into output_config so route defaults such as ``effort`` survive. The OpenAI
        # ``name`` and ``strict`` have no Anthropic equivalent (constrained decoding is always
        # strict) and are dropped.
        output_config = dict(request.get("output_config") or {})
        if "format" in output_config:
            raise ValueError("response_format conflicts with output_config.format")
        output_config["format"] = {"type": "json_schema", "schema": schema}
        request["output_config"] = output_config
    if body.get("stream"):
        request["stream"] = True
    if cache_ttl is not None:
        request["cache_control"] = (
            {"type": "ephemeral", "ttl": "1h"} if cache_ttl == "1h" else {"type": "ephemeral"}
        )
    return request


def _json_schema_of(response_format: Any) -> Any:
    """The JSON schema of a ``json_schema`` response format; ``None`` when none was asked."""
    if response_format is None:
        return None
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be an object")
    kind = response_format.get("type")
    if kind != "json_schema":
        raise ValueError(
            f"response_format type {kind!r} is not supported for anthropic_messages upstreams"
            " (a json_schema is required)"
        )
    spec = response_format.get("json_schema")
    schema = spec.get("schema") if isinstance(spec, dict) else None
    if not isinstance(schema, dict):
        raise ValueError("response_format.json_schema.schema is required")
    return schema


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def anthropic_usage_to_chat(raw_usage: Any) -> dict[str, Any]:
    """Anthropic usage -> chat usage.

    ``prompt_tokens`` counts every input token including cache writes and reads, because
    Anthropic's ``input_tokens`` is only the uncached remainder. Reads are reported in
    ``prompt_tokens_details.cached_tokens``; writes in the non-standard
    ``cache_creation_input_tokens`` so the gateway can price them.
    """
    usage_in: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    cache_write = _count(usage_in.get("cache_creation_input_tokens"))
    cache_read = _count(usage_in.get("cache_read_input_tokens"))
    prompt = _count(usage_in.get("input_tokens")) + cache_write + cache_read
    completion = _count(usage_in.get("output_tokens"))
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cache_read},
        "cache_creation_input_tokens": cache_write,
    }
    details = usage_in.get("output_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
        usage["completion_tokens_details"] = {"reasoning_tokens": details["thinking_tokens"]}
    return usage


def anthropic_response_to_chat(resp: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages response -> Chat Completions response (thinking blocks dropped)."""
    blocks = resp.get("content")
    text = "".join(
        block["text"]
        for block in (blocks if isinstance(blocks, list) else [])
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    usage = anthropic_usage_to_chat(resp.get("usage"))
    stop_reason = resp.get("stop_reason")
    return {
        "id": resp.get("id", ""),
        "object": "chat.completion",
        "created": 0,
        "model": resp.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _finish_reason(stop_reason),
            }
        ],
        "usage": usage,
    }


class AnthropicToChatStream:
    """Anthropic Messages SSE events -> Chat Completions chunks.

    ``feed(event)`` takes one decoded ``data:`` payload and returns the chat chunks it
    yields; ``finish()`` returns the closing usage-only chunk. Text deltas become
    ``delta.content`` and thinking deltas ``delta.reasoning_content``. ``usage`` holds the
    cumulative usage in chat shape (``message_start`` merged with the later
    ``message_delta``), and ``error`` is set when the upstream sent an ``error`` event.
    """

    def __init__(self, *, include_usage: bool) -> None:
        self.include_usage = include_usage
        self.usage: dict[str, Any] | None = None
        self.error: tuple[str, str] | None = None
        self._id = ""
        self._model = ""
        self._start_usage: dict[str, Any] = {}
        self._delta_usage: dict[str, Any] = {}

    def _chunk(
        self, choices: list[dict[str, Any]], usage: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self._model,
            "choices": choices,
        }
        if usage is not None:
            chunk["usage"] = usage
        return chunk

    def _delta(self, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return self._chunk([{"index": 0, "delta": delta, "finish_reason": finish_reason}])

    def _refresh_usage(self) -> None:
        merged = {
            **self._start_usage,
            **{k: v for k, v in self._delta_usage.items() if v is not None},
        }
        self.usage = anthropic_usage_to_chat(merged)

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            message = message if isinstance(message, dict) else {}
            self._id = str(message.get("id", ""))
            self._model = str(message.get("model", ""))
            usage = message.get("usage")
            self._start_usage = usage if isinstance(usage, dict) else {}
            self._refresh_usage()
            return [self._delta({"role": "assistant", "content": ""})]
        if kind == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return []
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [self._delta({"content": delta["text"]})]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [self._delta({"reasoning_content": delta["thinking"]})]
            return []
        if kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._delta_usage = usage
                self._refresh_usage()
            delta = event.get("delta")
            stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            return [self._delta({}, _finish_reason(stop_reason))]
        if kind == "error":
            error = event.get("error")
            error = error if isinstance(error, dict) else {}
            self.error = (
                str(error.get("type", "api_error")),
                str(error.get("message", "upstream error")),
            )
        return []

    def finish(self) -> list[dict[str, Any]]:
        if self.include_usage and self.usage is not None:
            return [self._chunk([], usage=self.usage)]
        return []
