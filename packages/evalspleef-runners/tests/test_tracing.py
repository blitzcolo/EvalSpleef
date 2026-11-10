"""Tracing: turn, model-call and tool spans; the gateway joins the runner's trace.

A real SDK provider with the in-memory exporter backs the assertions. The model
call sends the W3C traceparent to the gateway, so the gateway's server span and
upstream client span land in the runner's trace.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from evalspleef.runners.native_shell import ClaudeCodeRunner
from evalspleef.runners.unified_shell import UnifiedShellRunner
from evalspleef.spi.runner import StopReason
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_sandbox import MockSandbox

from .test_native import start_native
from .test_native import stub_cli as _stub_cli
from .test_unified import completion_payload, make_runner, start_runner, tool_call

# The stub CLI fixture lives in test_native; expose it to this module's tests.
stub_cli = _stub_cli


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """An SDK provider with the in-memory exporter, replacing the global state."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


def by_name(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


async def test_turn_model_call_and_tool_spans_carry_trial_attributes(
    spans: InMemorySpanExporter, sandbox: MockSandbox, budget: Any, tmp_path: Any
) -> None:
    with FakeOpenAIServer(model="ep_test") as server:
        server.queue_reply(tool_calls=(tool_call("call_1", "bash", {"command": "echo hi"}),))
        server.queue_reply(content="done")
        runner = make_runner(server, tmp_path, sandbox=sandbox, budget=budget)
        await start_runner(runner, server, sandbox, budget)
        turn = await runner.send("go", deadline_s=60)
        await runner.stop()
    assert turn.stop_reason is StopReason.COMPLETED

    (turn_span,) = by_name(spans, "runner.turn")
    turn_attributes = dict(turn_span.attributes or {})
    assert turn_attributes["runner"] == "terminus"
    assert turn_attributes["trial_id"] == "t1"
    assert turn_attributes["runner.stop_reason"] == "completed"

    model_spans = by_name(spans, "runner.model_call")
    assert len(model_spans) == 2  # one per gateway request
    for model_span in model_spans:
        attributes = dict(model_span.attributes or {})
        assert attributes["runner"] == "terminus"
        assert attributes["model"] == "ep_test"
        assert model_span.parent is not None
        assert model_span.parent.span_id == turn_span.context.span_id
    assert model_spans[0].attributes is not None
    assert "llm.usage.total_tokens" in model_spans[0].attributes

    (tool_span,) = by_name(spans, "runner.tool")
    tool_attributes = dict(tool_span.attributes or {})
    assert tool_attributes["tool"] == "bash"
    assert tool_attributes["trial_id"] == "t1"


async def test_model_call_sends_traceparent_and_the_gateway_joins_the_trace(
    spans: InMemorySpanExporter, sandbox: MockSandbox, budget: Any, tmp_path: Any
) -> None:
    """Runner -> gateway -> upstream over real apps: one trace id end to end."""
    from evalspleef.gateway.data_app import create_data_app
    from evalspleef.gateway.state import GatewayState
    from evalspleef.gateway.store import MemoryStore
    from evalspleef.spi.gateway import PriceSpec
    from evalspleef.spi.model import UpstreamRoute
    from evalspleef.testing.secrets import DictSecretResolver

    seen_headers: list[httpx.Headers] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, json=completion_payload("answer", []))

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
    assert turn.stop_reason is StopReason.COMPLETED

    # the upstream call carries the gateway's upstream span as its traceparent
    (headers,) = seen_headers
    upstream_traceparent = headers["traceparent"]

    model_span = by_name(spans, "runner.model_call")[0]
    gateway_span = by_name(spans, "gateway.request")[0]
    gateway_upstream = by_name(spans, "gateway.upstream")[0]
    runner_trace = f"{model_span.context.trace_id:032x}"
    # runner model call -> gateway server span (extract) -> upstream client span (inject)
    assert f"{gateway_span.context.trace_id:032x}" == runner_trace
    assert gateway_span.parent is not None
    assert gateway_span.parent.span_id == model_span.context.span_id
    assert f"{gateway_upstream.context.trace_id:032x}" == runner_trace
    assert gateway_upstream.parent is not None
    assert gateway_upstream.parent.span_id == gateway_span.context.span_id
    assert upstream_traceparent.split("-")[1] == runner_trace


async def test_native_shell_turn_span(
    spans: InMemorySpanExporter, sandbox: MockSandbox, stub_cli: Any
) -> None:
    runner = ClaudeCodeRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-claude")
    turn = await runner.send("hello", deadline_s=30)
    await runner.stop()
    assert turn.stop_reason is StopReason.COMPLETED

    (turn_span,) = by_name(spans, "runner.turn")
    attributes = dict(turn_span.attributes or {})
    assert attributes["runner"] == "claude-code"
    assert attributes["trial_id"] == "t1"
    assert attributes["runner.stop_reason"] == "completed"
