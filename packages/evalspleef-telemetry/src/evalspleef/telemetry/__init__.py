"""Shared OpenTelemetry setup for every spleef process.

Tracing stays a no-op unless an OTLP endpoint is configured through the standard
environment variables, so a plain run never needs a collector:

- ``OTEL_EXPORTER_OTLP_PROTOCOL``: ``grpc`` (the default, per the OTLP exporter
  specification) or ``http/protobuf``;
- ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``: where
  spans go; the ``_TRACES_`` variable wins and is used verbatim;
- ``OTEL_SERVICE_NAME``: overrides the service name the caller passes.

With no endpoint, :func:`configure_tracing` returns ``None`` and installs nothing.
When configured, spans leave the process through a batching processor; short-lived
commands must call :func:`flush_tracing` before returning (the SDK also shuts the
provider down at interpreter exit as a safety net).
"""

from .otlp import configure_tracing, flush_tracing, get_tracer

__all__ = ["configure_tracing", "flush_tracing", "get_tracer"]
