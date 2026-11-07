"""A fake OpenAI-compatible HTTP server for gateway, probe and runner tests.

``FakeOpenAIServer`` serves a real FastAPI app through uvicorn on 127.0.0.1 with an ephemeral
port, so clients exercise genuine HTTP (auth headers, SSE framing, status codes) without any
vendor. Behaviour is deterministic and scriptable:

- queued exact replies (``queue_reply``) for multi-turn scripts;
- rule-based tool calls (single, parallel, ``tool_choice`` ``required``/``none`` and a named
  function), with a text answer once a tool result comes back;
- ``reasoning_content`` on/off per server and per request, ``response_format`` ``json_schema``
  for simple schemas, ``seed`` determinism, ``stop`` sequences and ``max_tokens`` with
  ``finish_reason`` ``length``;
- a context limit with needle recall (echoes the ``needle:`` value from the user message) and
  HTTP 400 ``context_length_exceeded`` beyond it;
- injected latency and injected errors (next N calls, or a clock-measured window);
- Bearer auth check, configurable reported model and system fingerprint, deterministic
  whitespace token accounting and a request log for assertions.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .clock import Clock, SystemClock

_DEFAULT_REPLY = "fake reply"
_NEEDLE = re.compile(r"needle[:=]\s*(\S+)")
_ERRORS: dict[int, tuple[str, str]] = {
    429: ("rate_limit_error", "rate_limit_exceeded"),
    500: ("server_error", "internal_server_error"),
    503: ("server_error", "service_unavailable"),
}


def count_tokens(text: str) -> int:
    """Deterministic token accounting: whitespace-separated words."""
    return len(text.split())


@dataclass(frozen=True)
class RecordedRequest:
    """One chat completion request as the server received it."""

    path: str
    body: dict[str, Any]


@dataclass(frozen=True)
class ScriptedReply:
    """One exact assistant message the server emits, bypassing all rule-based generation."""

    content: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    finish_reason: str = "stop"
    reasoning_content: str | None = None


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, Mapping))
    return ""


def _messages(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = body.get("messages")
    if not isinstance(raw, list):
        return []
    return [message for message in raw if isinstance(message, Mapping)]


def _fill_schema(schema: Mapping[str, Any]) -> Any:
    """Produce schema-valid JSON for simple (type/properties) JSON schemas."""
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return {}
        return {str(name): _fill_schema(value) for name, value in properties.items()}
    if kind == "array":
        items = schema.get("items")
        return [_fill_schema(items if isinstance(items, Mapping) else {})]
    if kind == "string":
        return "x"
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return None


def _tool_arguments(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build example arguments for a tool schema: required properties, or all of them."""
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    names = (
        [str(name) for name in required]
        if isinstance(required, (list, tuple))
        else [str(name) for name in properties]
    )
    arguments: dict[str, Any] = {}
    for name in names:
        value = properties.get(name)
        arguments[name] = _fill_schema(value if isinstance(value, Mapping) else {})
    return arguments


class FakeOpenAIServer:
    """Scriptable OpenAI-compatible server. Use as a sync or async context manager."""

    def __init__(
        self,
        *,
        model: str = "fake-model",
        reported_model: str | None = None,
        system_fingerprint: str | None = None,
        api_key: str | None = None,
        clock: Clock | None = None,
        context_limit_tokens: int | None = None,
        parallel_tool_calls: bool = False,
        reasoning_content: bool = False,
        long_output_words: int | None = None,
    ) -> None:
        self.model = model
        self.reported_model = reported_model if reported_model is not None else model
        self.system_fingerprint = system_fingerprint
        self.api_key = api_key
        self.context_limit_tokens = context_limit_tokens
        self.parallel_tool_calls = parallel_tool_calls
        self.reasoning_enabled = reasoning_content
        self.long_output_words = long_output_words
        self.latency_s = 0.0
        self.requests: list[RecordedRequest] = []
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._scripted: deque[ScriptedReply] = deque()
        self._pending_errors: deque[int] = deque()
        self._error_windows: list[tuple[float, int]] = []
        self._response_counter = 0
        self._app = FastAPI(title="evalspleef-fake-openai")
        self._app.add_api_route("/v1/models", self._models, methods=["GET"])
        self._app.add_api_route("/v1/chat/completions", self._chat, methods=["POST"])
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None

    # -- context managers ------------------------------------------------------

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("server is not started")
        return self._port

    @property
    def base_url(self) -> str:
        """The OpenAI client base URL (``http://127.0.0.1:<port>/v1``)."""
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        if self._server is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        config = uvicorn.Config(self._app, log_level="error", access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="fake-openai",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                listener.close()
                raise RuntimeError("fake OpenAI server failed to start")
            time.sleep(0.005)
        self._socket = listener
        self._server = server
        self._thread = thread
        self._port = listener.getsockname()[1]

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._socket is not None:
            self._socket.close()
        self._socket = None
        self._server = None
        self._thread = None
        self._port = None

    def __enter__(self) -> FakeOpenAIServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    async def __aenter__(self) -> FakeOpenAIServer:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.stop()

    # -- test-side scripting ----------------------------------------------------

    def queue_reply(
        self,
        content: str | None = None,
        *,
        tool_calls: tuple[Mapping[str, Any], ...] = (),
        finish_reason: str = "stop",
        reasoning_content: str | None = None,
    ) -> None:
        """Queue one exact assistant message; queued replies take precedence over rules."""
        self._scripted.append(
            ScriptedReply(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                reasoning_content=reasoning_content,
            )
        )

    def inject_error(self, status: int, *, count: int = 1) -> None:
        """Return HTTP ``status`` (429/500/503) for the next ``count`` chat calls."""
        if status not in _ERRORS:
            raise ValueError(f"unsupported injected status: {status}")
        self._pending_errors.extend([status] * count)

    def inject_error_for(self, status: int, *, seconds: float) -> None:
        """Return HTTP ``status`` for chat calls during the next ``seconds`` of clock time."""
        if status not in _ERRORS:
            raise ValueError(f"unsupported injected status: {status}")
        self._error_windows.append((self._clock.now() + seconds, status))

    # -- HTTP endpoints -----------------------------------------------------------

    async def _models(self, request: Request) -> Response:
        del request
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": self.model, "object": "model", "created": 0, "owned_by": "evalspleef"}
                ],
            }
        )

    async def _chat(self, request: Request) -> Response:
        raw = await request.json()
        assert isinstance(raw, dict)
        body: dict[str, Any] = raw
        self.requests.append(RecordedRequest(path=request.url.path, body=body))

        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error
        injected = self._injected_status()
        if injected is not None:
            error_type, code = _ERRORS[injected]
            return JSONResponse(
                {
                    "error": {
                        "message": f"injected error {injected}",
                        "type": error_type,
                        "code": code,
                    }
                },
                status_code=injected,
            )
        if self.latency_s > 0:
            await asyncio.sleep(self.latency_s)

        messages = _messages(body)
        prompt_tokens = sum(count_tokens(_message_text(message)) for message in messages)
        if self.context_limit_tokens is not None and prompt_tokens > self.context_limit_tokens:
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            f"prompt has {prompt_tokens} tokens, "
                            f"limit is {self.context_limit_tokens}"
                        ),
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                    }
                },
                status_code=400,
            )

        message, finish_reason, completion_tokens = self._generate(body)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            return StreamingResponse(
                self._stream_chunks(message, finish_reason, usage, include_usage),
                media_type="text/event-stream",
            )
        return JSONResponse(self._completion(message, finish_reason, usage))

    # -- request helpers ------------------------------------------------------------

    def _check_auth(self, request: Request) -> JSONResponse | None:
        if self.api_key is None:
            return None
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return JSONResponse(
                {
                    "error": {
                        "message": "invalid api key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
                status_code=401,
            )
        return None

    def _injected_status(self) -> int | None:
        if self._pending_errors:
            return self._pending_errors.popleft()
        now = self._clock.now()
        active = [(until, status) for until, status in self._error_windows if until > now]
        self._error_windows = active
        if active:
            return active[0][1]
        return None

    def _next_id(self) -> str:
        self._response_counter += 1
        return f"chatcmpl-{self._response_counter}"

    # -- response generation ---------------------------------------------------------

    def _responds_with_tool_calls(self, body: Mapping[str, Any]) -> bool:
        tools = body.get("tools")
        if not isinstance(tools, list) or not tools:
            return False
        choice = body.get("tool_choice", "auto")
        if choice == "none":
            return False
        if choice == "required" or isinstance(choice, Mapping):
            return True
        return self.parallel_tool_calls

    def _tool_calls(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        tools = [tool for tool in body.get("tools") or [] if isinstance(tool, Mapping)]
        choice = body.get("tool_choice")
        if isinstance(choice, Mapping):
            function = choice.get("function")
            named = function.get("name") if isinstance(function, Mapping) else None
            matching = [
                tool
                for tool in tools
                if isinstance(tool.get("function"), Mapping)
                and tool["function"].get("name") == named
            ]
            tools = matching or tools
        if not self.parallel_tool_calls:
            tools = tools[:1]
        calls: list[dict[str, Any]] = []
        for index, tool in enumerate(tools):
            function = tool.get("function")
            function = function if isinstance(function, Mapping) else {}
            parameters = function.get("parameters")
            arguments = _tool_arguments(parameters if isinstance(parameters, Mapping) else {})
            calls.append(
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": str(function.get("name", "")),
                        "arguments": json.dumps(arguments),
                    },
                }
            )
        return calls

    def _default_text(self) -> str:
        if self.long_output_words is not None:
            return " ".join(["word"] * self.long_output_words)
        return _DEFAULT_REPLY

    def _needle(self, messages: Sequence[Mapping[str, Any]]) -> str | None:
        for message in messages:
            if message.get("role") != "user":
                continue
            match = _NEEDLE.search(_message_text(message))
            if match is not None:
                return match.group(1)
        return None

    def _generate(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], str, int]:
        """Produce the assistant message, finish reason and completion token count."""
        messages = _messages(body)
        message: dict[str, Any] = {"role": "assistant", "content": None}
        finish_reason = "stop"
        scripted = self._scripted.popleft() if self._scripted else None

        if scripted is not None:
            message["content"] = scripted.content
            if scripted.tool_calls:
                message["tool_calls"] = [dict(call) for call in scripted.tool_calls]
            if scripted.reasoning_content is not None:
                message["reasoning_content"] = scripted.reasoning_content
            completion_tokens = count_tokens(scripted.content or "")
            return message, scripted.finish_reason, completion_tokens

        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if tool_messages:
            content = f"tool-result:{len(tool_messages)}"
            message["content"] = content
        elif self._responds_with_tool_calls(body):
            message["tool_calls"] = self._tool_calls(body)
            finish_reason = "tool_calls"
            completion_tokens = sum(
                count_tokens(str(call["function"]["arguments"])) for call in message["tool_calls"]
            )
            self._maybe_add_reasoning(body, message)
            return message, finish_reason, completion_tokens
        else:
            response_format = body.get("response_format")
            if (
                isinstance(response_format, Mapping)
                and response_format.get("type") == "json_schema"
            ):
                json_schema = response_format.get("json_schema")
                schema = json_schema.get("schema") if isinstance(json_schema, Mapping) else None
                content = json.dumps(_fill_schema(schema if isinstance(schema, Mapping) else {}))
            else:
                needle = self._needle(messages)
                content = f"needle: {needle}" if needle is not None else self._default_text()
            message["content"] = content

        self._maybe_add_reasoning(body, message)

        content = message["content"]
        if not isinstance(content, str):
            return message, finish_reason, 0
        text = content
        seed = body.get("seed")
        if seed is not None:
            text = f"{text} [seed={seed}]"
        stops = body.get("stop")
        if isinstance(stops, str):
            stops = [stops]
        if isinstance(stops, list):
            cut_at: int | None = None
            for stop in stops:
                index = text.find(str(stop))
                if index != -1 and (cut_at is None or index < cut_at):
                    cut_at = index
            if cut_at is not None:
                text = text[:cut_at]
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, int):
            words = text.split()
            if len(words) > max_tokens:
                text = " ".join(words[:max_tokens])
                finish_reason = "length"
        message["content"] = text
        return message, finish_reason, count_tokens(text)

    def _maybe_add_reasoning(self, body: Mapping[str, Any], message: dict[str, Any]) -> None:
        if not self.reasoning_enabled:
            return
        reasoning = body.get("reasoning")
        if isinstance(reasoning, Mapping) and reasoning.get("enabled") is False:
            return
        message["reasoning_content"] = "thinking about the request"

    # -- response payloads ------------------------------------------------------------

    def _envelope(self, *, chunk: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self._next_id(),
            "object": "chat.completion.chunk" if chunk else "chat.completion",
            "created": int(time.time()),
            "model": self.reported_model,
        }
        if self.system_fingerprint is not None:
            payload["system_fingerprint"] = self.system_fingerprint
        return payload

    def _completion(
        self,
        message: Mapping[str, Any],
        finish_reason: str,
        usage: Mapping[str, int],
    ) -> dict[str, Any]:
        payload = self._envelope(chunk=False)
        payload["choices"] = [
            {"index": 0, "message": dict(message), "finish_reason": finish_reason}
        ]
        payload["usage"] = dict(usage)
        return payload

    async def _stream_chunks(
        self,
        message: Mapping[str, Any],
        finish_reason: str,
        usage: Mapping[str, int],
        include_usage: bool,
    ) -> AsyncIterator[str]:
        base = self._envelope(chunk=True)

        def event(payload: Mapping[str, Any]) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        def delta_chunk(delta: Mapping[str, Any], fr: str | None) -> str:
            choices = [{"index": 0, "delta": dict(delta), "finish_reason": fr}]
            return event({**base, "choices": choices})

        yield delta_chunk({"role": "assistant"}, None)
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            yield delta_chunk({"reasoning_content": reasoning}, None)
        content = message.get("content")
        if isinstance(content, str) and content:
            for index, word in enumerate(content.split(" ")):
                piece = word if index == 0 else f" {word}"
                yield delta_chunk({"content": piece}, None)
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            yield delta_chunk({"tool_calls": [{**dict(tool_call), "index": index}]}, None)
        yield delta_chunk({}, finish_reason)
        if include_usage:
            yield event({**base, "choices": [], "usage": dict(usage)})
        yield "data: [DONE]\n\n"
