"""configure_tracing: protocol and endpoint selection, no-op default, export."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer
from opentelemetry.util._once import Once

from evalspleef.telemetry import configure_tracing, flush_tracing, get_tracer
from evalspleef.telemetry import otlp as otlp_module


@pytest.fixture
def pristine_otel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An unconfigured global OTel state; the real one is restored afterwards."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(otlp_module, "_installed", None)
    yield
    installed = otlp_module._installed
    if installed is not None:
        installed.shutdown()


@pytest.fixture
def selected(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Records which OTLP exporter factory the next configure_tracing call uses."""
    chosen: dict[str, str] = {}

    def fake_grpc(endpoint: str) -> SpanExporter:
        chosen["kind"] = "grpc"
        return InMemorySpanExporter()

    def fake_http(endpoint: str) -> SpanExporter:
        chosen["kind"] = "http"
        return InMemorySpanExporter()

    monkeypatch.setattr(otlp_module, "_grpc_exporter", fake_grpc)
    monkeypatch.setattr(otlp_module, "_http_exporter", fake_http)
    return chosen


def test_without_endpoint_nothing_changes(pristine_otel: None) -> None:
    assert configure_tracing("svc", environ={}) is None
    assert configure_tracing("svc", environ={"OTEL_EXPORTER_OTLP_ENDPOINT": ""}) is None
    assert isinstance(get_tracer("x"), NoOpTracer)
    assert flush_tracing() is True


@pytest.mark.parametrize("protocol", ["", "grpc"])
def test_grpc_is_the_default_protocol(
    pristine_otel: None, selected: dict[str, str], protocol: str
) -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317"}
    if protocol:
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = protocol
    provider = configure_tracing("svc", environ=env)
    assert isinstance(provider, TracerProvider)
    assert selected["kind"] == "grpc"


def test_http_protocol_selects_the_http_exporter(
    pristine_otel: None, selected: dict[str, str]
) -> None:
    configure_tracing(
        "svc",
        environ={
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        },
    )
    assert selected["kind"] == "http"


def test_unknown_protocol_falls_back_to_grpc(pristine_otel: None, selected: dict[str, str]) -> None:
    configure_tracing(
        "svc",
        environ={
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
        },
    )
    assert selected["kind"] == "grpc"


def test_importing_telemetry_does_not_import_grpc() -> None:
    """The OTLP/gRPC stack stays lazy: importing this package must not pull in grpc.

    An eager grpc import registers the C-core fork handlers process-wide, and any
    later fork (e.g. a local-exec sandbox) then inherits gRPC log noise on stderr.
    """
    repo_root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [sys.executable, "-c", "import evalspleef.telemetry, sys; sys.exit('grpc' in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_http_url_rules() -> None:
    assert (
        otlp_module._endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"}, "http/protobuf")
        == "http://c:4318/v1/traces"
    )
    assert (
        otlp_module._endpoint(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t:9/custom",
            },
            "http/protobuf",
        )
        == "http://t:9/custom"
    )
    assert (
        otlp_module._endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4317"}, "grpc")
        == "http://c:4317"
    )
    assert otlp_module._endpoint({}, "grpc") is None


def test_service_name(pristine_otel: None) -> None:
    provider = configure_tracing(
        "svc", environ={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}
    )
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "svc"


def test_otel_service_name_env_wins(pristine_otel: None) -> None:
    provider = configure_tracing(
        "svc",
        environ={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
            "OTEL_SERVICE_NAME": "operator-override",
        },
    )
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "operator-override"


def test_configure_is_idempotent(pristine_otel: None) -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}
    first = configure_tracing("svc", environ=env, exporter=InMemorySpanExporter())
    second = configure_tracing("svc", environ=env, exporter=InMemorySpanExporter())
    assert first is not None
    assert second is first


def test_provider_exports_finished_spans(pristine_otel: None) -> None:
    exporter = InMemorySpanExporter()
    provider = configure_tracing(
        "svc", environ={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}, exporter=exporter
    )
    assert provider is not None
    assert trace.get_tracer_provider() is provider
    with get_tracer("probe").start_as_current_span("probe") as span:
        span.set_attribute("op", "probe")
    assert flush_tracing() is True
    assert [s.name for s in exporter.get_finished_spans()] == ["probe"]
