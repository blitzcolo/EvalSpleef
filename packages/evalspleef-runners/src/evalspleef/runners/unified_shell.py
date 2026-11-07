"""Unified shell runner (slug ``terminus``): the platform-owned agent loop.

The loop lives outside the sandbox in ``remote-tools`` mode. Model calls go to the
gateway with the per-trial key over the chat-completions protocol; tool calls are
executed inside the sandbox through the ``Sandbox`` SPI, so the sandbox itself can
stay fully offline (TECH-DESIGN section 8.2).

Tools: ``bash``, ``read``, ``write``, ``edit`` and ``ask_user``. Each tool is a thin
mapping onto SPI calls — the loop never invents tool results, and malformed
tool-call JSON is handed back to the model as a tool error. Stops, heartbeats and
loop detection follow PRD appendix B.3.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

import httpx
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import Sandbox

from .common import (
    Clock,
    Heartbeat,
    LoopDetector,
    SystemClock,
    ToolArgumentsError,
    TranscriptWriter,
    parse_tool_arguments,
    require_str,
    tool_call_key,
)

SYSTEM_PROMPT = (
    "You are a software engineering agent operating in a sandboxed Linux workspace at "
    "/workspace. Use the provided tools to inspect and modify files and run commands. "
    "When the task is complete, reply with a short summary of what you did and stop."
)

# The gateway reports per-request cost in this response header; absent means 0.
COST_HEADER = "x-spleef-cost"

# Caps for tool-level bounds so a model cannot request an unbounded sandbox exec.
DEFAULT_MAX_TOOL_TIMEOUT_S = 600.0
DEFAULT_REQUEST_TIMEOUT_S = 300.0
DEFAULT_MAX_REQUEST_RETRIES = 3

WORKSPACE = "/workspace"

_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace and return its exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout_s": {
                        "type": "integer",
                        "description": "Timeout in seconds (bounded by the runner).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a text file from the workspace and return its content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file in the workspace, replacing existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write."},
                    "content": {"type": "string", "description": "Full new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exactly one occurrence of a text span in a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit."},
                    "old": {
                        "type": "string",
                        "description": "Exact text to replace (must appear once).",
                    },
                    "new": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a clarifying question when the task cannot proceed without input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask."}
                },
                "required": ["question"],
            },
        },
    },
]


def _error_code(exc: Exception) -> str | None:
    """Extract a machine error code from an OpenAI API error body, if present."""
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str):
            return code
    code = body.get("code")
    if isinstance(code, str):
        return code
    return None


class UnifiedShellRunner(AgentRunner):
    """The platform agent loop driving sandbox tools through the gateway."""

    name = "terminus"
    version = "0.1.0"
    supported_modes = frozenset({"remote-tools"})
    required_protocol = "chat_completions"

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        transcript_dir: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        idle_turns: int = 2,
        loop_threshold: int = 3,
        max_tool_timeout_s: float = DEFAULT_MAX_TOOL_TIMEOUT_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        max_request_retries: int = DEFAULT_MAX_REQUEST_RETRIES,
        heartbeat_interval_s: float = 15.0,
        heartbeat: Any | None = None,
    ) -> None:
        if idle_turns < 1:
            raise ValueError("idle_turns must be >= 1")
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._transcript_dir = (
            Path(transcript_dir) if transcript_dir is not None else Path(tempfile.gettempdir())
        )
        self._http_client = http_client
        self._idle_turns = idle_turns
        self._loop_detector = LoopDetector(loop_threshold)
        self._max_tool_timeout_s = max_tool_timeout_s
        self._request_timeout_s = request_timeout_s
        self._max_request_retries = max_request_retries
        self._heartbeat_interval_s = heartbeat_interval_s
        # Optional liveness callback invoked by the heartbeat; kept injectable so the
        # control plane can wire it to its own reporting without this package
        # depending on it.
        self._on_heartbeat = heartbeat

        self._sandbox: Sandbox | None = None
        self._budget: RunnerBudget | None = None
        self._model_alias = ""
        self._messages: list[ChatCompletionMessageParam] = []
        self._client: AsyncOpenAI | None = None
        self._transcript: TranscriptWriter | None = None
        self._total_tokens = 0
        self._total_cost = 0.0
        self._idle_streak = 0
        self._started_at: float | None = None
        self._cancelled = False
        self._stopped = False
        self.last_error: str | None = None

    # -- AgentRunner -----------------------------------------------------------

    async def start(
        self,
        sandbox: Sandbox,
        *,
        gateway_url: str,
        trial_key: str,
        model_alias: str,
        instruction: str,
        budget: RunnerBudget,
    ) -> None:
        self._sandbox = sandbox
        self._budget = budget
        self._model_alias = model_alias
        self._started_at = self._clock.now()
        self._client = AsyncOpenAI(
            base_url=gateway_url,
            api_key=trial_key,
            timeout=self._request_timeout_s,
            # The SDK types its http_client against a vendored httpx copy; any
            # httpx.AsyncClient is accepted at runtime.
            http_client=cast(Any, self._http_client),
        )
        self._transcript_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self._transcript_dir / f"terminus-{uuid.uuid4().hex}.jsonl"
        self._transcript = TranscriptWriter(trace_path, secrets=(trial_key,))
        self._messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        await self._transcript.record({"type": "turn", "event": "start", "message": instruction})
        self._stopped = False

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        if self._sandbox is None or self._client is None or self._budget is None:
            raise RuntimeError("runner is not started")
        budget = self._budget
        turn_start = self._clock.now()
        started_at = self._started_at if self._started_at is not None else turn_start
        deadline = min(turn_start + deadline_s, started_at + budget.max_wall_clock_s)

        self._messages.append({"role": "user", "content": message})
        if self._transcript is not None:
            await self._transcript.record({"type": "message", "role": "user", "content": message})

        steps = 0
        usage = TokenUsage()
        stop = StopReason.CRASH
        final_message: str | None = None
        cost_at_start = self._total_cost

        async with Heartbeat(self._clock, self._heartbeat_interval_s, self._beat):
            while True:
                if self._cancelled:
                    stop = StopReason.CANCELLED
                    break
                if self._clock.now() >= deadline:
                    stop = StopReason.TIMEOUT
                    break
                if self._total_tokens >= budget.max_total_tokens:
                    stop = StopReason.TOKEN_CAP
                    break
                if self._total_cost >= budget.max_cost_usd:
                    stop = StopReason.COST_CAP
                    break

                try:
                    completion, cost = await self._request()
                except _FatalRequestError as exc:
                    self.last_error = str(exc)
                    stop = exc.stop_reason
                    break
                except Exception as exc:  # surfaced as the crash stop reason
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    stop = StopReason.CRASH
                    break

                self._total_cost += cost
                message_out = completion.choices[0].message
                usage = self._accumulate_usage(usage, completion)
                await self._append_assistant(message_out)

                tool_calls = message_out.tool_calls or []
                content = message_out.content or ""
                if not tool_calls:
                    if content.strip():
                        self._idle_streak = 0
                        stop = StopReason.COMPLETED
                        final_message = content
                        break
                    self._idle_streak += 1
                    if self._idle_streak >= self._idle_turns:
                        stop = StopReason.IDLE
                        break
                    # Empty assistant turn: keep looping so the model can act again.
                    continue

                self._idle_streak = 0
                clarification: str | None = None
                for call in tool_calls:
                    steps += 1
                    if call.type != "function":
                        await self._append_tool(call.id, "tool error: unsupported tool call type")
                        continue
                    name = call.function.name
                    try:
                        arguments = parse_tool_arguments(call.function.arguments or "{}")
                    except ToolArgumentsError as exc:
                        await self._append_tool(call.id, f"tool error: {exc}")
                        continue
                    if self._loop_detector.observe(tool_call_key(name, arguments)):
                        stop = StopReason.LOOP_DETECTED
                        break
                    result = await self._dispatch(name, arguments)
                    if isinstance(result, _Clarification):
                        clarification = result.question
                        break
                    await self._append_tool(call.id, result)
                if clarification is not None:
                    stop = StopReason.NEED_CLARIFICATION
                    final_message = clarification
                    break
                if stop is StopReason.LOOP_DETECTED:
                    break

        duration = self._clock.now() - turn_start
        turn = ShellTurn(
            stop_reason=stop,
            final_message=final_message,
            steps=steps,
            usage=usage,
            cost_usd=self._total_cost - cost_at_start,
            duration_s=duration,
            trace_ref=str(self._transcript.path) if self._transcript is not None else None,
        )
        if self._transcript is not None:
            await self._transcript.record(
                {
                    "type": "turn",
                    "event": "end",
                    "stop_reason": str(stop),
                    "steps": steps,
                    "final_message": final_message,
                }
            )
        return turn

    async def stop(self) -> None:
        """Idempotent: close the transcript and release the model client."""
        if self._stopped:
            return
        self._stopped = True
        if self._transcript is not None:
            self._transcript.close()
        if self._client is not None and self._http_client is None:
            await self._client.close()

    def cancel(self) -> None:
        """Ask the running ``send`` loop to end with ``StopReason.CANCELLED``."""
        self._cancelled = True

    # -- model calls -------------------------------------------------------------

    async def _request(self) -> tuple[ChatCompletion, float]:
        """One chat-completions call; returns (completion, cost from the x-spleef header)."""
        assert self._client is not None
        attempt = 0
        while True:
            try:
                raw = await self._client.chat.completions.with_raw_response.create(
                    model=self._model_alias,
                    messages=self._messages,
                    tools=cast(Any, _TOOL_SCHEMAS),
                )
                completion = raw.parse()
                header = raw.headers.get(COST_HEADER)
                try:
                    cost = float(header) if header is not None else 0.0
                except ValueError:
                    cost = 0.0
                return completion, cost
            except RateLimitError as exc:
                code = _error_code(exc)
                if code == "cost_cap":
                    raise _FatalRequestError(
                        "gateway reported cost cap", StopReason.COST_CAP
                    ) from exc
                if code == "token_cap":
                    raise _FatalRequestError(
                        "gateway reported token cap", StopReason.TOKEN_CAP
                    ) from exc
                if attempt >= self._max_request_retries:
                    raise _FatalRequestError(
                        f"rate limit persisted: {exc}", StopReason.CRASH
                    ) from exc
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= self._max_request_retries:
                    raise _FatalRequestError(
                        f"model request failed: {exc}", StopReason.CRASH
                    ) from exc
            attempt += 1
            await self._clock.sleep(min(2.0**attempt, 8.0))

    def _accumulate_usage(self, usage: TokenUsage, completion: ChatCompletion) -> TokenUsage:
        api_usage = completion.usage
        if api_usage is None:
            return usage
        cached = 0
        details = api_usage.prompt_tokens_details
        if details is not None and details.cached_tokens is not None:
            cached = details.cached_tokens
        reasoning = 0
        completion_details = api_usage.completion_tokens_details
        if completion_details is not None and completion_details.reasoning_tokens is not None:
            reasoning = completion_details.reasoning_tokens
        turn = TokenUsage(
            input_tokens=usage.input_tokens + api_usage.prompt_tokens,
            output_tokens=usage.output_tokens + api_usage.completion_tokens,
            cached_tokens=usage.cached_tokens + cached,
            reasoning_tokens=usage.reasoning_tokens + reasoning,
        )
        self._total_tokens += api_usage.prompt_tokens + api_usage.completion_tokens
        return turn

    # -- conversation --------------------------------------------------------------

    async def _append_assistant(self, message: Any) -> None:
        entry: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
                if call.type == "function"
            ]
        self._messages.append(entry)  # type: ignore[arg-type]
        if self._transcript is not None:
            await self._transcript.record({"type": "message", **entry})

    async def _append_tool(self, call_id: str, content: str) -> None:
        self._messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if self._transcript is not None:
            await self._transcript.record(
                {"type": "message", "role": "tool", "tool_call_id": call_id, "content": content}
            )

    def _beat(self) -> None:
        if self._on_heartbeat is not None:
            self._on_heartbeat()

    # -- tools ------------------------------------------------------------------------

    async def _dispatch(self, name: str, arguments: Mapping[str, Any]) -> str | _Clarification:
        sandbox = self._require_sandbox()
        try:
            if name == "bash":
                return await self._tool_bash(sandbox, arguments)
            if name == "read":
                return await self._tool_read(sandbox, arguments)
            if name == "write":
                return await self._tool_write(sandbox, arguments)
            if name == "edit":
                return await self._tool_edit(sandbox, arguments)
            if name == "ask_user":
                return _Clarification(require_str(arguments, "question"))
        except ToolArgumentsError as exc:
            return f"tool error: {exc}"
        return f"tool error: unknown tool: {name}"

    def _require_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("runner is not started")
        return self._sandbox

    async def _tool_bash(self, sandbox: Sandbox, arguments: Mapping[str, Any]) -> str:
        command = require_str(arguments, "command")
        timeout_s = float(arguments.get("timeout_s", 60))
        timeout_s = max(1.0, min(timeout_s, self._max_tool_timeout_s))
        result = await sandbox.exec(
            command,
            cwd=WORKSPACE,
            user="agent",
            timeout_s=timeout_s,
        )
        parts = [f"exit_code: {result.exit_code}"]
        if result.timed_out:
            parts.append("timed_out: true")
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)

    async def _tool_read(self, sandbox: Sandbox, arguments: Mapping[str, Any]) -> str:
        path = self._workspace_path(require_str(arguments, "path"))
        result = await sandbox.exec(["cat", self._arg_path(path)], cwd=WORKSPACE, user="agent")
        if result.exit_code != 0:
            return (
                f"tool error: cat {path} failed: {result.stderr.decode('utf-8', errors='replace')}"
            )
        return result.stdout.decode("utf-8", errors="replace")

    async def _tool_write(self, sandbox: Sandbox, arguments: Mapping[str, Any]) -> str:
        path = self._workspace_path(require_str(arguments, "path"))
        content = require_str(arguments, "content")
        await self._atomic_replace(sandbox, path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def _tool_edit(self, sandbox: Sandbox, arguments: Mapping[str, Any]) -> str:
        path = self._workspace_path(require_str(arguments, "path"))
        old = require_str(arguments, "old")
        new = require_str(arguments, "new")
        current = await self._tool_read(sandbox, {"path": path})
        if current.startswith("tool error:"):
            return current
        occurrences = current.count(old)
        if occurrences != 1:
            return f"tool error: text to replace appears {occurrences} times (expected exactly 1)"
        await self._atomic_replace(sandbox, path, current.replace(old, new))
        return f"edited {path}"

    async def _atomic_replace(self, sandbox: Sandbox, path: str, content: str) -> None:
        """Upload a temp file, then mv it over the target for an atomic replace."""
        staging = f".spleef-staging-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(content)
            local = Path(handle.name)
        try:
            await sandbox.upload(local, f"{WORKSPACE}/{staging}")
        finally:
            await asyncio.to_thread(local.unlink, missing_ok=True)
        target = self._arg_path(path)
        parent = str(PurePosixPath(target).parent)
        if parent not in (".", "/"):
            await sandbox.exec(["mkdir", "-p", parent], cwd=WORKSPACE, user="agent")
        moved = await sandbox.exec(["mv", staging, target], cwd=WORKSPACE, user="agent")
        if moved.exit_code != 0:
            raise RuntimeError(
                f"atomic replace failed for {path}: {moved.stderr.decode('utf-8', errors='replace')}"
            )

    @staticmethod
    def _arg_path(path: str) -> str:
        """Path as passed to sandbox exec argv.

        Sandboxes that remap the workspace root only translate ``cwd`` (the mock
        sandbox works this way), so exec arguments stay relative to /workspace.
        """
        if path == WORKSPACE:
            return "."
        prefix = f"{WORKSPACE}/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        return path

    @staticmethod
    def _workspace_path(path: str) -> str:
        """Anchor relative tool paths at the workspace root."""
        if path.startswith("/"):
            return path
        return f"{WORKSPACE}/{path}"


class _Clarification:
    def __init__(self, question: str) -> None:
        self.question = question


class _FatalRequestError(Exception):
    """A model-request failure that maps directly onto a stop reason."""

    def __init__(self, message: str, stop_reason: StopReason) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
