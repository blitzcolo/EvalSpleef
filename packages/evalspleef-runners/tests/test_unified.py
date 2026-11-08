"""Tests for ``UnifiedShellRunner`` against FakeOpenAIServer and MockSandbox.

The full agent loop runs over real HTTP against the fake OpenAI-compatible server;
sandbox tool calls run for real in the mock sandbox temp directory. Deterministic
time behaviour comes from FakeClock and scripted replies.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from evalspleef.runners.unified_shell import COST_HEADER, UnifiedShellRunner
from evalspleef.spi.runner import RunnerBudget, StopReason
from evalspleef.spi.sandbox import Sandbox
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_sandbox import MockSandbox

TRIAL_KEY = "trial-key-0123456789abcdef"  # deliberately not sk-shaped


def tool_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def make_runner(
    server: FakeOpenAIServer,
    tmp_path: Path,
    *,
    sandbox: Sandbox,
    budget: Any,
    clock: FakeClock | None = None,
    http_client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> UnifiedShellRunner:
    runner = UnifiedShellRunner(
        clock=clock, transcript_dir=tmp_path, http_client=http_client, **kwargs
    )
    return runner


async def start_runner(
    runner: UnifiedShellRunner,
    server: FakeOpenAIServer,
    sandbox: Sandbox,
    budget: Any,
) -> None:
    await runner.start(
        sandbox,
        gateway_url=server.base_url,
        trial_key=TRIAL_KEY,
        model_alias=server.model,
        instruction="do the task",
        budget=budget,
    )


async def sandbox_file(sandbox: Sandbox, path: str) -> str:
    """Read a sandbox file; the mock sandbox remaps cwd, not argv paths."""
    prefix = "/workspace/"
    if path.startswith(prefix):
        result = await sandbox.exec(["cat", path[len(prefix) :]], cwd="/workspace")
    else:
        result = await sandbox.exec(["cat", path])
    assert result.exit_code == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("utf-8", errors="replace")


async def wait_for_condition(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Await a state condition with a bounded timeout (no fixed waits)."""

    async def _poll() -> None:
        while not condition():  # noqa: ASYNC110
            # The condition is produced by sync server-side code, so an Event
            # cannot be wired; yield briefly and re-check.
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout_s)


async def read_text(path: str | Path) -> str:
    return await asyncio.to_thread(Path(path).read_text, encoding="utf-8")


async def file_exists(path: str) -> bool:
    return await asyncio.to_thread(Path(path).exists)


async def test_full_loop_two_tools_then_final_answer(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(
            tool_calls=(tool_call("call_1", "bash", {"command": "echo hello > out.txt"}),),
            finish_reason="tool_calls",
        )
        server.queue_reply(
            tool_calls=(tool_call("call_2", "read", {"path": "out.txt"}),),
            finish_reason="tool_calls",
        )
        server.queue_reply(content="all done")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("please proceed", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert turn.final_message == "all done"
        assert turn.steps == 2
        assert turn.usage.input_tokens > 0
        assert turn.usage.output_tokens > 0
        assert turn.trace_ref is not None
        assert await file_exists(turn.trace_ref)
        assert await sandbox_file(sandbox, "/workspace/out.txt") == "hello\n"
        # The trial key and the model saw the neutral system prompt only.
        transcript = await read_text(turn.trace_ref)
        assert TRIAL_KEY not in transcript
        assert "ep_test" not in transcript.splitlines()[0]  # system prompt is neutral
        await runner.stop()


async def test_write_and_edit_tools(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(
            tool_calls=(
                tool_call("call_1", "write", {"path": "notes.txt", "content": "alpha beta"}),
            ),
            finish_reason="tool_calls",
        )
        server.queue_reply(
            tool_calls=(
                tool_call("call_2", "edit", {"path": "notes.txt", "old": "beta", "new": "gamma"}),
            ),
            finish_reason="tool_calls",
        )
        server.queue_reply(content="written and edited")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert await sandbox_file(sandbox, "/workspace/notes.txt") == "alpha gamma"
        await runner.stop()


async def test_malformed_tool_call_becomes_tool_error(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(
            tool_calls=(
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{not json"},
                },
            ),
            finish_reason="tool_calls",
        )
        server.queue_reply(content="recovered")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert turn.final_message == "recovered"
        # The error was returned to the model as a real tool message.
        second_request = server.requests[1].body
        tool_messages = [
            message for message in second_request["messages"] if message.get("role") == "tool"
        ]
        assert len(tool_messages) == 1
        assert "tool error" in str(tool_messages[0]["content"])
        await runner.stop()


async def test_unknown_tool_returns_error(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(
            tool_calls=(tool_call("call_1", "browser", {"url": "http://x"}),),
            finish_reason="tool_calls",
        )
        server.queue_reply(content="ok")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert "unknown tool" in server.requests[1].body["messages"][-1]["content"]
        await runner.stop()


async def test_stop_reason_idle(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(content=None)
        server.queue_reply(content=None)
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget, idle_turns=2)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.IDLE
        await runner.stop()


async def test_stop_reason_need_clarification(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(
            tool_calls=(tool_call("call_1", "ask_user", {"question": "which port should I use?"}),),
            finish_reason="tool_calls",
        )
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.NEED_CLARIFICATION
        assert turn.final_message == "which port should I use?"
        await runner.stop()


async def test_stop_reason_loop_detected(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        for index in range(4):
            server.queue_reply(
                tool_calls=(tool_call(f"call_{index}", "bash", {"command": "echo hi"}),),
                finish_reason="tool_calls",
            )
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget, loop_threshold=3)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.LOOP_DETECTED
        await runner.stop()


async def test_stop_reason_cancelled(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        for index in range(5):
            server.queue_reply(
                tool_calls=(tool_call(f"call_{index}", "bash", {"command": f"echo {index}"}),),
                finish_reason="tool_calls",
            )
        server.queue_reply(content="finished")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        send_task = asyncio.create_task(runner.send("go", deadline_s=60))
        await wait_for_condition(lambda: len(server.requests) >= 2)
        runner.cancel()
        turn = await send_task

        assert turn.stop_reason is StopReason.CANCELLED
        await runner.stop()


async def test_stop_reason_timeout_via_injected_clock(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(content="too late")
        clock = FakeClock(start=1000.0)
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget, clock=clock)
        await start_runner(runner, server, sandbox, budget)
        clock.advance(5000.0)  # blow through both the send deadline and the budget

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.TIMEOUT
        assert len(server.requests) == 0  # the loop stopped before any model call
        await runner.stop()


async def test_stop_reason_token_cap_local_counter(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        # A tool round-trip first: a direct final answer would legitimately end as
        # COMPLETED before the budget check runs again.
        server.queue_reply(
            tool_calls=(tool_call("call_1", "bash", {"command": "echo hi"}),),
            finish_reason="tool_calls",
        )
        server.queue_reply(content="second")
        small = RunnerBudget(max_total_tokens=1, max_cost_usd=100.0, max_wall_clock_s=3600)
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=small)
        await start_runner(runner, server, sandbox, small)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.TOKEN_CAP
        assert len(server.requests) == 1
        await runner.stop()


async def test_stop_reason_crash_on_persistent_500(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.inject_error(500, count=3)
        runner = make_runner(
            server, tmp_path, sandbox=sandbox, budget=budget, max_request_retries=0
        )
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.CRASH
        assert runner.last_error is not None
        await runner.stop()


@pytest.mark.parametrize(
    ("error_code", "stop_reason"),
    [("cost_cap", StopReason.COST_CAP), ("token_cap", StopReason.TOKEN_CAP)],
)
async def test_gateway_429_error_codes_map_to_stop_reasons(
    sandbox: MockSandbox, budget: Any, tmp_path: Path, error_code: str, stop_reason: StopReason
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": f"gateway budget stop: {error_code}",
                    "type": "rate_limit_error",
                    "code": error_code,
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        runner = UnifiedShellRunner(transcript_dir=tmp_path, http_client=http_client)
        await runner.start(
            sandbox,
            gateway_url="http://gateway.test/v1",
            trial_key=TRIAL_KEY,
            model_alias="ep_test",
            instruction="do the task",
            budget=budget,
        )

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is stop_reason
        await runner.stop()


def completion_payload(content: str | None, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "ep_test",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


async def test_cost_from_x_spleef_cost_header(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            payload = completion_payload(
                None, [tool_call("call_1", "bash", {"command": "echo hi"})]
            )
        else:
            payload = completion_payload("priced answer", [])
        return httpx.Response(200, json=payload, headers={COST_HEADER: "0.21"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        runner = UnifiedShellRunner(transcript_dir=tmp_path, http_client=http_client)
        await runner.start(
            sandbox,
            gateway_url="http://gateway.test/v1",
            trial_key=TRIAL_KEY,
            model_alias="ep_test",
            instruction="do the task",
            budget=budget,
        )

        turn = await runner.send("go", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert turn.final_message == "priced answer"
        assert abs(turn.cost_usd - 0.42) < 1e-9
        await runner.stop()


async def test_cost_charged_by_the_real_gateway_reaches_the_turn(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    """The runner books what the gateway data plane charged, not a hand-set header."""
    from evalspleef.gateway.data_app import create_data_app
    from evalspleef.gateway.state import GatewayState
    from evalspleef.gateway.store import MemoryStore
    from evalspleef.spi.gateway import PriceSpec
    from evalspleef.spi.model import UpstreamRoute
    from evalspleef.testing.secrets import DictSecretResolver

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = completion_payload("priced answer", [])
        payload["usage"] = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        return httpx.Response(200, json=payload)

    store = MemoryStore()
    state = GatewayState(
        key_store=store, usage_store=store, secrets=DictSecretResolver({"vault://up": "k"})
    )
    state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    state.upsert_route(
        "ep_test",
        UpstreamRoute(
            api_base="http://upstream.test/v1",
            model="up-model",
            protocol="chat_completions",
            api_key_ref="vault://up",
        ),
    )
    state.set_price("ep_test", PriceSpec(input_per_mtok=2.0, output_per_mtok=8.0))
    key = await state.issue_key(
        trial_id="t1",
        endpoint_id="ep_test",
        max_budget_usd=10.0,
        max_tokens=1_000_000,
        expires_at="2099-01-01T00:00:00Z",
    )
    gateway = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_data_app(state)))
    async with gateway:
        runner = UnifiedShellRunner(transcript_dir=tmp_path, http_client=gateway)
        await runner.start(
            sandbox,
            gateway_url="http://gateway.test/v1",
            trial_key=key.secret,
            model_alias="ep_test",
            instruction="do the task",
            budget=budget,
        )
        turn = await runner.send("go", deadline_s=60)
        await runner.stop()

    # 1000 * $2/M + 500 * $8/M, exactly what the gateway recorded for the trial.
    assert turn.stop_reason is StopReason.COMPLETED
    assert turn.cost_usd == pytest.approx(0.006)
    assert [u.cost_usd for u in await state.usage_for("t1")] == [pytest.approx(0.006)]


async def test_conversation_persists_across_send_calls(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(content="first answer")
        server.queue_reply(content="second answer")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        first = await runner.send("first message", deadline_s=60)
        second = await runner.send("feedback: improve it", deadline_s=60)

        assert first.stop_reason is StopReason.COMPLETED
        assert second.stop_reason is StopReason.COMPLETED
        assert second.final_message == "second answer"
        last_request = server.requests[-1].body
        roles = [message["role"] for message in last_request["messages"]]
        assert roles.count("user") == 3  # instruction + both send() messages
        assert any(
            message.get("content") == "feedback: improve it" for message in last_request["messages"]
        )
        await runner.stop()


async def test_sk_shaped_secrets_redacted_from_transcript(
    sandbox: MockSandbox, budget: Any, tmp_path: Path
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(content="done")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)

        turn = await runner.send("my key is sk-abcdef1234567890XYZ keep it safe", deadline_s=60)

        assert turn.stop_reason is StopReason.COMPLETED
        assert turn.trace_ref is not None
        transcript = await read_text(turn.trace_ref)
        assert "sk-abcdef1234567890XYZ" not in transcript
        assert "***REDACTED***" in transcript
        await runner.stop()


async def test_heartbeat_callback_fires(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        # Keep the model request in flight across the heartbeat interval so the
        # FakeClock advance deterministically lands mid-turn.
        server.latency_s = 1.0
        server.queue_reply(content="done")
        beats: list[float] = []
        clock = FakeClock(start=0.0)
        runner = UnifiedShellRunner(clock=clock, transcript_dir=tmp_path)
        assert runner.watch_heartbeat(lambda: beats.append(clock.now()))
        await runner.start(
            sandbox,
            gateway_url=server.base_url,
            trial_key=TRIAL_KEY,
            model_alias=server.model,
            instruction="do the task",
            budget=budget,
        )

        send_task = asyncio.create_task(runner.send("go", deadline_s=60))
        await wait_for_condition(lambda: len(server.requests) >= 1)
        clock.advance(15.0)
        turn = await send_task

        assert turn.stop_reason is StopReason.COMPLETED
        assert beats == [15.0]
        await runner.stop()


async def test_send_before_start_raises(sandbox: MockSandbox, budget: Any, tmp_path: Path) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        with pytest.raises(RuntimeError):
            await runner.send("go", deadline_s=60)
