"""Install the OpenTelemetry SDK provider for the short-lived ``spleef`` CLI.

The setup itself lives in :mod:`evalspleef.telemetry` so the gateway, the runners
and hostd share one implementation; this module pins the control plane's service
name and keeps the call sites unchanged. Tracing stays a no-op unless an OTLP
endpoint is configured through the standard environment variables (see
:func:`evalspleef.telemetry.configure_tracing`).
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from evalspleef.telemetry import configure_tracing as _configure
from evalspleef.telemetry import flush_tracing

__all__ = ["SERVICE_NAME", "configure_tracing", "flush_tracing"]

SERVICE_NAME = "spleef-control"


def configure_tracing(
    environ: Mapping[str, str] | None = None, *, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Install the SDK tracer provider when an OTLP endpoint is configured.

    Thin wrapper over :func:`evalspleef.telemetry.configure_tracing` with the
    control plane's service name; see there for the variable semantics.
    """
    return _configure(SERVICE_NAME, environ=environ, exporter=exporter)
