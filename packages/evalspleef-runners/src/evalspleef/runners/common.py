"""Shared helpers for agent runners.

This module holds the pieces every runner needs regardless of execution mode:

- ``parse_tool_arguments`` — strict parsing of model-emitted tool-call JSON. The
  agent loop never invents tool results, so a malformed call becomes a tool error
  that is handed back to the model verbatim.
- ``LoopDetector`` — PRD appendix B.3 ``loop_detected``: the same tool call
  repeated N consecutive times ends the turn.
- ``Heartbeat`` — PRD appendix B.3 heartbeat: the runner reports liveness every
  15 s of *injected-clock* time, so tests never wait on wall time.
- ``TranscriptWriter`` — append-only JSONL transcript with secrets redacted
  (the trial key and anything matching an ``sk-`` pattern).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

REDACTED = "***REDACTED***"

# API-key shaped literals: sk- followed by at least 8 key characters. The lower bound
# avoids redacting innocent hyphenated words that start with "sk-".
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

# PRD appendix B.3: the shell reports a heartbeat every 15 s.
HEARTBEAT_INTERVAL_S = 15.0


class Clock(Protocol):
    """Time source; satisfied by SystemClock/FakeClock in evalspleef-testing."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall-clock implementation used in production."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace API-key-shaped literals and any known secret values."""
    redacted = _SK_PATTERN.sub(REDACTED, text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_obj(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact strings inside a JSON-serialisable structure."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {key: redact_obj(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, secrets) for item in value]
    return value


class ToolArgumentsError(ValueError):
    """Raised when a model-emitted tool call carries malformed arguments."""


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse the ``arguments`` JSON of a tool call.

    Raises ``ToolArgumentsError`` when the payload is not a JSON object; the agent
    loop turns that into a tool error message for the model instead of guessing.
    """
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolArgumentsError(f"tool arguments are not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolArgumentsError("tool arguments must be a JSON object")
    return value


def require_str(arguments: Mapping[str, Any], name: str) -> str:
    """Fetch a required string tool argument, raising ``ToolArgumentsError`` otherwise."""
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ToolArgumentsError(f"missing or invalid string argument: {name}")
    return value


def require_int(arguments: Mapping[str, Any], name: str) -> int:
    """Fetch a required integer tool argument, raising ``ToolArgumentsError`` otherwise."""
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentsError(f"missing or invalid integer argument: {name}")
    return value


def tool_call_key(name: str, arguments: Mapping[str, Any]) -> str:
    """Canonical key for loop detection: tool name plus normalised arguments."""
    return f"{name} {json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


class LoopDetector:
    """Fires when the same tool call is observed ``threshold`` times in a row."""

    def __init__(self, threshold: int) -> None:
        if threshold < 1:
            raise ValueError("loop threshold must be >= 1")
        self._threshold = threshold
        self._last: str | None = None
        self._streak = 0

    def observe(self, key: str) -> bool:
        """Record one tool call; returns True when the loop threshold is reached."""
        if key == self._last:
            self._streak += 1
        else:
            self._last = key
            self._streak = 1
        return self._streak >= self._threshold

    def reset(self) -> None:
        self._last = None
        self._streak = 0


BeatCallback = Callable[[], Awaitable[None] | None]


class Heartbeat:
    """Invoke ``on_beat`` every ``interval_s`` of clock time until stopped.

    Runs as an async context manager; cancellation propagates through the
    injected clock's ``sleep``, so a ``FakeClock`` test drives beats via
    ``advance`` with no wall-time waits.
    """

    def __init__(self, clock: Clock, interval_s: float, on_beat: BeatCallback) -> None:
        self._clock = clock
        self._interval_s = interval_s
        self._on_beat = on_beat
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Heartbeat:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self._clock.sleep(self._interval_s)
            result = self._on_beat()
            if inspect.isawaitable(result):
                await result


class TranscriptWriter:
    """Append-only JSONL transcript with secret redaction applied to every line."""

    def __init__(self, path: Path, secrets: Iterable[str] = ()) -> None:
        self._path = path
        self._secrets = tuple(secret for secret in secrets if secret)
        self._lock = asyncio.Lock()
        self._file = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    async def record(self, event: Mapping[str, Any]) -> None:
        """Append one redacted JSON event."""
        line = json.dumps(redact_obj(dict(event), self._secrets), ensure_ascii=False)
        async with self._lock:
            await asyncio.to_thread(self._file.write, line + "\n")
            await asyncio.to_thread(self._file.flush)

    def close(self) -> None:
        self._file.close()
