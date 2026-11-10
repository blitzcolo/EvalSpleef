"""Tracing: one server span per data-plane request, a client span per upstream call.

A real SDK provider with the in-memory exporter backs the assertions: the server
span carries route, flavor, status and token attributes; the upstream client span
is its child; an incoming W3C traceparent joins the request to the caller's trace
and the gateway re-injects the context for the upstream call.
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
from opentelemetry.trace import SpanContext
from opentelemetry.util._once import Once

from evalspleef.gateway.tracing import SERVER_SPAN, UPSTREAM_SPAN

from .conftest import UPSTREAM_KEY, auth
from .test_data_app import add_route, canned_chat, client_for, issue

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"


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


async def test_server_span_per_request_with_route_and_usage_attributes(
    spans: InMemorySpanExporter, state: Any
) -> None:
    add_route(state)
    secret = await issue(state)

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned_chat(prompt_tokens=11, completion_tokens=2))

    async with client_for(state, upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": []},
            headers=auth(secret),
        )
        assert response.status_code == 200

    (server,) = by_name(spans, SERVER_SPAN)
    assert server.attributes is not None
    attributes = dict(server.attributes)
    assert attributes["url.path"] == "/v1/chat/completions"
    assert attributes["gateway.flavor"] == "openai"
    assert attributes["http.response.status_code"] == 200
    assert attributes["gateway.endpoint_id"] == "ep_ep1"
    assert attributes["gateway.status_code"] == 200
    assert attributes["gateway.input_tokens"] == 11
    assert attributes["gateway.output_tokens"] == 2
    assert "gateway.key_id" in attributes
    # key material never appears in span attributes
    for value in attributes.values():
        assert value != secret


async def test_incoming_traceparent_parents_the_server_span(
    spans: InMemorySpanExporter, state: Any
) -> None:
    add_route(state)
    secret = await issue(state)
    async with client_for(state, lambda request: httpx.Response(200, json=canned_chat())) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": []},
            headers={**auth(secret), "traceparent": TRACEPARENT},
        )
        assert response.status_code == 200

    (server,) = by_name(spans, SERVER_SPAN)
    parent = server.parent
    assert isinstance(parent, SpanContext)
    assert f"{parent.trace_id:032x}" == TRACE_ID
    assert f"{parent.span_id:016x}" == PARENT_ID


async def test_upstream_client_span_is_the_server_spans_child_and_injects_traceparent(
    spans: InMemorySpanExporter, state: Any
) -> None:
    add_route(state)
    secret = await issue(state)
    seen_headers: list[httpx.Headers] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, json=canned_chat())

    async with client_for(state, upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ep_ep1", "messages": []},
            headers={**auth(secret), "traceparent": TRACEPARENT},
        )
        assert response.status_code == 200

    (server,) = by_name(spans, SERVER_SPAN)
    (upstream_span,) = by_name(spans, UPSTREAM_SPAN)
    assert upstream_span.parent is not None
    assert upstream_span.parent.span_id == server.context.span_id
    assert upstream_span.attributes is not None
    assert upstream_span.attributes["http.response.status_code"] == 200
    assert upstream_span.attributes["gateway.upstream_retries"] == 0
    # the upstream request carries the gateway's client span as its traceparent
    (headers,) = seen_headers
    traceparent = headers["traceparent"]
    _, header_trace, header_parent, _flags = traceparent.split("-")
    assert header_trace == TRACE_ID  # same trial trace the runner started
    assert header_parent == f"{upstream_span.context.span_id:016x}"
    assert headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
