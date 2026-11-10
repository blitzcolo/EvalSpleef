"""Tracing: one server span per Hostd RPC, linked to the RemoteProvider client span.

A real SDK provider with the in-memory exporter backs the assertions over a real
loopback mTLS stack: create/exec/destroy produce client spans (RemoteProvider
channel) and server spans (hostd interceptor) that share one trace, and the RPC
request's sandbox id lands on the server span.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.util._once import Once

from evalspleef.hostd.remote import RemoteSandbox
from evalspleef.spi.errors import SandboxLost

from .conftest import SpecMaker, Stacks


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


def by_method(exporter: InMemorySpanExporter, suffix: str) -> list[Any]:
    return [span for span in exporter.get_finished_spans() if span.name.endswith(suffix)]


async def test_create_exec_destroy_produce_linked_client_and_server_spans(
    spans: InMemorySpanExporter, stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _provider = await stacks.start()
    remote = stack.remote("mock")
    sandbox = await remote.create(spec("t-1"))
    result = await sandbox.exec("echo hi", timeout_s=10)
    assert result.exit_code == 0
    await sandbox.destroy()
    await remote.aclose()

    for suffix in ("/Create", "/Exec", "/Destroy"):
        server_spans = [span for span in by_method(spans, suffix) if span.kind is SpanKind.SERVER]
        client_spans = [span for span in by_method(spans, suffix) if span.kind is SpanKind.CLIENT]
        assert len(server_spans) == 1, f"server spans for {suffix}: {server_spans}"
        assert len(client_spans) == 1, f"client spans for {suffix}: {client_spans}"
        server, client = server_spans[0], client_spans[0]
        # the client interceptor injected traceparent; the server extracted it
        assert server.context.trace_id == client.context.trace_id
        assert server.parent is not None
        assert server.parent.span_id == client.context.span_id
        assert server.parent.is_remote

    (exec_server,) = [span for span in by_method(spans, "/Exec") if span.kind is SpanKind.SERVER]
    attributes = dict(exec_server.attributes or {})
    assert attributes["sandbox_id"] == sandbox.id


async def test_rpc_status_is_recorded_on_the_server_span(
    spans: InMemorySpanExporter, stacks: Stacks, spec: SpecMaker
) -> None:
    stack, _provider = await stacks.start()
    remote = stack.remote("mock")
    lost = RemoteSandbox(remote, "sb-does-not-exist", spec("t-1"))
    with pytest.raises(SandboxLost):
        await lost.snapshot("x")
    await remote.aclose()

    (server,) = [span for span in by_method(spans, "/Snapshot") if span.kind is SpanKind.SERVER]
    attributes = dict(server.attributes or {})
    assert attributes["rpc.grpc.status_code"] != 0
