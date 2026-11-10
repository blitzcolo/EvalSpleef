"""Install the OpenTelemetry SDK provider selected by the standard OTLP variables.

The protocol comes from ``OTEL_EXPORTER_OTLP_PROTOCOL`` (``grpc`` by default per
the OTLP exporter specification, ``http/protobuf`` as the alternative); the
endpoint from ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (verbatim) or
``OTEL_EXPORTER_OTLP_ENDPOINT`` (used as-is for gRPC, with ``/v1/traces`` appended
for HTTP). With no endpoint set, nothing is installed and tracing stays a no-op.

The exporters are imported lazily, only when their protocol is actually selected:
a process that never configures tracing (or only the other protocol) must not pull
in the gRPC stack at import time.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

__all__ = ["configure_tracing", "flush_tracing", "get_tracer"]

_PROTOCOL_VAR = "OTEL_EXPORTER_OTLP_PROTOCOL"
_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
_TRACES_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_SERVICE_NAME_VAR = "OTEL_SERVICE_NAME"
_FLUSH_TIMEOUT_MS = 10_000

GRPC = "grpc"
HTTP = "http/protobuf"

_lock = threading.Lock()
_installed: TracerProvider | None = None


def _grpc_exporter(endpoint: str) -> SpanExporter:
    """The OTLP/gRPC exporter, imported only when gRPC is the selected protocol."""
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)


def _http_exporter(endpoint: str) -> SpanExporter:
    """The OTLP/HTTP exporter, imported only when HTTP is the selected protocol."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)


def _protocol(environ: Mapping[str, str]) -> str:
    """The OTLP protocol; unknown values fall back to the spec default (gRPC)."""
    raw = environ.get(_PROTOCOL_VAR, "")
    if raw in ("", GRPC):
        return GRPC
    if raw == HTTP:
        return HTTP
    return GRPC


def _endpoint(environ: Mapping[str, str], protocol: str) -> str | None:
    """Where spans are exported: the traces variable verbatim, else the generic one.

    For HTTP the generic endpoint gets ``/v1/traces`` appended, per the OTLP
    exporter specification; the traces-specific variable is used as given.
    """
    specific = environ.get(_TRACES_ENDPOINT_VAR)
    if specific:
        return specific
    generic = environ.get(_ENDPOINT_VAR)
    if not generic:
        return None
    if protocol == HTTP:
        return generic.rstrip("/") + "/v1/traces"
    return generic


def configure_tracing(
    service_name: str,
    *,
    environ: Mapping[str, str] | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install the SDK tracer provider when an OTLP endpoint is configured.

    Returns the installed provider, or ``None`` (and changes nothing) when neither
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` nor ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
    Idempotent: a second call returns the provider the first one installed, and an
    SDK provider installed by someone else is adopted rather than replaced.
    ``exporter`` replaces the OTLP exporter (tests use an in-memory one).
    """
    global _installed
    env = os.environ if environ is None else environ
    protocol = _protocol(env)
    endpoint = _endpoint(env, protocol)
    if endpoint is None:
        return None
    with _lock:
        if _installed is not None:
            return _installed
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            _installed = current
            return current
        name = env.get(_SERVICE_NAME_VAR) or service_name
        provider = TracerProvider(resource=Resource.create({"service.name": name}))
        if exporter is None:
            exporter = _grpc_exporter(endpoint) if protocol == GRPC else _http_exporter(endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _installed = provider
        return provider


def get_tracer(name: str, version: str = "") -> trace.Tracer:
    """Return a tracer for ``name``; no-op until an SDK provider is configured.

    The global provider is a proxy/no-op placeholder until the application calls
    ``trace.set_tracer_provider`` with an SDK provider; only then do spans become
    real, recording spans.
    """
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider | trace.NoOpTracerProvider):
        return trace.NoOpTracer()
    return provider.get_tracer(name, version)


def flush_tracing() -> bool:
    """Export every finished span now; True when nothing is pending or tracing is off."""
    provider = _installed
    if provider is None:
        return True
    return provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
