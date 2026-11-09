"""Install the OpenTelemetry SDK provider for the short-lived ``spleef`` CLI.

Tracing stays a no-op unless an OTLP endpoint is configured through the standard
environment variables, so a plain run never needs a collector. When it is
configured, spans leave the process through a batching processor; because the CLI
exits right after a command, callers must :func:`flush_tracing` before returning
(the SDK also shuts the provider down at interpreter exit as a safety net).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

__all__ = ["SERVICE_NAME", "configure_tracing", "flush_tracing"]

SERVICE_NAME = "spleef"

_TRACES_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
_FLUSH_TIMEOUT_MS = 10_000

_lock = threading.Lock()
_installed: TracerProvider | None = None


def _traces_url(environ: Mapping[str, str]) -> str | None:
    """The OTLP/HTTP traces URL, or None when tracing is not configured."""
    specific = environ.get(_TRACES_ENDPOINT_VAR)
    if specific:
        return specific  # used verbatim, per the OTLP exporter specification
    generic = environ.get(_ENDPOINT_VAR)
    if generic:
        return generic.rstrip("/") + "/v1/traces"
    return None


def configure_tracing(
    environ: Mapping[str, str] | None = None, *, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Install the SDK tracer provider when an OTLP endpoint is configured.

    Returns the installed provider, or None (and changes nothing) when neither
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` nor ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
    Idempotent: a second call returns the provider the first one installed, and an
    SDK provider installed by someone else is adopted rather than replaced.
    ``exporter`` replaces the OTLP exporter (tests use an in-memory one).
    """
    global _installed
    env = os.environ if environ is None else environ
    url = _traces_url(env)
    if url is None:
        return None
    with _lock:
        if _installed is not None:
            return _installed
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            _installed = current
            return current
        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        span_exporter = exporter if exporter is not None else OTLPSpanExporter(endpoint=url)
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        _installed = provider
        return provider


def flush_tracing() -> bool:
    """Export every finished span now; True when nothing is pending or tracing is off."""
    provider = _installed
    if provider is None:
        return True
    return provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
