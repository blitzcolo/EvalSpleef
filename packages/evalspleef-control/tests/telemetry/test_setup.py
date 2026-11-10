"""configure_tracing: a no-op without an OTLP endpoint, one SDK provider with it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer
from opentelemetry.util._once import Once

from evalspleef.control.telemetry import configure_tracing, flush_tracing, tracer
from evalspleef.telemetry import otlp as shared_otlp


@pytest.fixture
def pristine_otel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An unconfigured global OTel state; the real one is restored afterwards."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(shared_otlp, "_installed", None)
    yield
    installed = shared_otlp._installed
    if installed is not None:
        installed.shutdown()


def test_without_endpoint_nothing_changes(pristine_otel: None) -> None:
    assert configure_tracing({}) is None
    assert configure_tracing({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}) is None
    assert isinstance(tracer(), NoOpTracer)
    assert flush_tracing() is True


@pytest.mark.parametrize(
    "variable", ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
)
def test_with_endpoint_installs_a_provider_that_exports(pristine_otel: None, variable: str) -> None:
    exporter = InMemorySpanExporter()
    provider = configure_tracing({variable: "http://127.0.0.1:1"}, exporter=exporter)
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "spleef-control"
    assert trace.get_tracer_provider() is provider

    with tracer().start_as_current_span("probe") as span:
        span.set_attribute("op", "probe")
    # batched: nothing is lost once the CLI flushes before it exits
    assert flush_tracing() is True
    assert [s.name for s in exporter.get_finished_spans()] == ["probe"]


def test_configure_is_idempotent(pristine_otel: None) -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}
    first = configure_tracing(env, exporter=InMemorySpanExporter())
    second = configure_tracing(env, exporter=InMemorySpanExporter())
    assert first is not None
    assert second is first
