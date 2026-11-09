"""HTTP access to the gateway data plane for the probe suite.

Probes go through the gateway with a probe key so they validate the whole chain, gateway
plus upstream (TECH-DESIGN section 6.4). Every request carries explicit timeouts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

from evalspleef.core.perf import RequestSample
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.gateway import COLD_START_HEADER

from .clock import Clock

__all__ = [
    "CHAT_PATH",
    "MESSAGES_PATH",
    "RESPONSES_PATH",
    "GatewayClient",
    "ProbeResponseError",
    "Reply",
    "StreamReply",
    "as_dict",
    "as_list",
]

CHAT_PATH = "/v1/chat/completions"
MESSAGES_PATH = "/v1/messages"
RESPONSES_PATH = "/v1/responses"


class ProbeResponseError(SpleefError):
    """The gateway answered with a payload the probe cannot interpret."""

    error_class = "provider"
    disposition = "manual"


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _error_text(body: Mapping[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        label = error.get("code") or error.get("type") or "error"
        return f"{label}: {error.get('message', '')}"
    return error if isinstance(error, str) else ""


@dataclass(frozen=True)
class Reply:
    """A non-streaming gateway answer with chat-completions accessors."""

    status: int
    body: dict[str, Any]
    headers: Mapping[str, str]
    latency_s: float

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def error(self) -> str:
        return f"HTTP {self.status} {_error_text(self.body)}".strip()

    @property
    def cold_start_ms(self) -> int:
        try:
            return int(self.headers.get(COLD_START_HEADER, "0"))
        except ValueError:
            return 0

    @property
    def message(self) -> dict[str, Any]:
        choices = as_list(self.body.get("choices"))
        choice = as_dict(choices[0]) if choices else {}
        return as_dict(choice.get("message"))

    @property
    def content(self) -> str:
        value = self.message.get("content")
        return value if isinstance(value, str) else ""

    @property
    def finish_reason(self) -> str | None:
        choices = as_list(self.body.get("choices"))
        value = as_dict(choices[0]).get("finish_reason") if choices else None
        return value if isinstance(value, str) else None

    @property
    def completion_tokens(self) -> int | None:
        value = as_dict(self.body.get("usage")).get("completion_tokens")
        return value if isinstance(value, int) else None


@dataclass(frozen=True)
class StreamReply:
    """A parsed SSE chat completion stream."""

    status: int
    chunks: tuple[dict[str, Any], ...]
    done: bool
    delta_chunks: int
    ttft_s: float | None
    latency_s: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def usage(self) -> dict[str, Any]:
        for chunk in reversed(self.chunks):
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                return usage
        return {}


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        return as_dict(response.json())
    except ValueError:
        return {}


def _has_delta(chunk: Mapping[str, Any]) -> bool:
    choices = as_list(chunk.get("choices"))
    delta = as_dict(as_dict(choices[0]).get("delta")) if choices else {}
    return any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls"))


def _has_generated_text(chunk: Mapping[str, Any]) -> bool:
    """The TTFT/ITL definition: a chunk whose delta carries content or reasoning text."""
    for choice in as_list(chunk.get("choices")):
        delta = as_dict(as_dict(choice).get("delta"))
        if any(
            isinstance(delta.get(key), str) and delta.get(key)
            for key in ("content", "reasoning_content")
        ):
            return True
    return False


def _usage_tokens(usage: Mapping[str, Any]) -> tuple[int, int, int]:
    """(input, cached, output) tokens of a chat usage object; reasoning is part of output."""

    def number(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    details = as_dict(usage.get("prompt_tokens_details"))
    return (
        number(usage.get("prompt_tokens")),
        number(details.get("cached_tokens")),
        number(usage.get("completion_tokens")),
    )


class GatewayClient:
    """Bearer-authenticated client for one gateway; use as an async context manager."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        clock: Clock,
        request_timeout_s: float,
        connect_timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._request_timeout_s = request_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=httpx.Timeout(request_timeout_s, connect=connect_timeout_s),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    def _timeout(self, timeout_s: float | None) -> httpx.Timeout:
        read_s = self._request_timeout_s if timeout_s is None else timeout_s
        return httpx.Timeout(read_s, connect=self._connect_timeout_s)

    async def post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        started = self._clock.now()
        response = await self._http.post(
            path, json=dict(body), headers=headers, timeout=self._timeout(timeout_s)
        )
        return Reply(
            status=response.status_code,
            body=_json_body(response),
            headers={key.lower(): value for key, value in response.headers.items()},
            latency_s=self._clock.now() - started,
        )

    async def stream_sample(
        self, body: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> RequestSample:
        """Send one streaming chat request and keep the raw timing record of it.

        Every chunk carrying generated text is stamped on this client's clock as it arrives.
        A transport failure, a non-200 answer, an error event or a stream without a usage
        chunk is a failed sample (it stops a saturation sweep), never an exception.
        """
        started = self._clock.now()
        chunk_times: list[float] = []
        usage: dict[str, Any] = {}
        error: str | None = None
        try:
            async with self._http.stream(
                "POST", CHAT_PATH, json=dict(body), timeout=self._timeout(timeout_s)
            ) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    try:
                        detail = _error_text(as_dict(json.loads(raw)))
                    except ValueError:
                        detail = raw.decode("utf-8", errors="replace")[:200]
                    error = f"HTTP {response.status_code} {detail}".strip()
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:") :].strip()
                        if payload == "[DONE]":
                            break
                        arrived = self._clock.now()
                        try:
                            chunk = as_dict(json.loads(payload))
                        except ValueError:
                            error = f"malformed SSE frame: {payload[:80]!r}"
                            break
                        if chunk.get("error"):
                            error = f"stream error: {_error_text(chunk)}"
                            break
                        if _has_generated_text(chunk):
                            chunk_times.append(arrived)
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                    if error is None and not usage:
                        error = "no usage chunk in the stream"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
        input_tokens, cached_tokens, output_tokens = _usage_tokens(usage)
        return RequestSample(
            t_start=started,
            t_end=self._clock.now(),
            chunk_times=tuple(chunk_times),
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            ok=error is None,
            streamed=True,
            error=error,
        )

    async def stream_chat(
        self, body: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> StreamReply:
        """POST a streaming chat request and parse the SSE frames."""
        started = self._clock.now()
        chunks: list[dict[str, Any]] = []
        done = False
        delta_chunks = 0
        ttft_s: float | None = None
        async with self._http.stream(
            "POST", CHAT_PATH, json=dict(body), timeout=self._timeout(timeout_s)
        ) as response:
            if response.status_code != 200:
                raw = await response.aread()
                try:
                    error = _error_text(as_dict(json.loads(raw)))
                except ValueError:
                    error = raw.decode("utf-8", errors="replace")[:200]
                return StreamReply(
                    status=response.status_code,
                    chunks=(),
                    done=False,
                    delta_chunks=0,
                    ttft_s=None,
                    latency_s=self._clock.now() - started,
                    error=error,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError as exc:
                    raise ProbeResponseError(f"malformed SSE frame: {payload[:80]!r}") from exc
                if not isinstance(chunk, dict):
                    raise ProbeResponseError(f"SSE frame is not an object: {payload[:80]!r}")
                chunks.append(chunk)
                if _has_delta(chunk):
                    delta_chunks += 1
                    if ttft_s is None:
                        ttft_s = self._clock.now() - started
        return StreamReply(
            status=200,
            chunks=tuple(chunks),
            done=done,
            delta_chunks=delta_chunks,
            ttft_s=ttft_s,
            latency_s=self._clock.now() - started,
        )
