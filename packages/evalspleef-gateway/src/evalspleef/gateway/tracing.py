"""OpenTelemetry spans for the gateway data plane.

One server span per data-plane request, joined to the caller's trace through the
W3C ``traceparent`` header, plus one client span per upstream call that injects
the context again so the model endpoint can join too. Attributes carry only ids,
token counts and costs - never key material (a trial key appears only as its key
id, and only when it is already an id).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from evalspleef.telemetry import get_tracer

SERVICE_NAME = "spleef-gateway"

SERVER_SPAN = "gateway.request"
UPSTREAM_SPAN = "gateway.upstream"


def tracer() -> trace.Tracer:
    """The gateway tracer; no-op until an SDK provider is configured."""
    return get_tracer("evalspleef.gateway")


def extract_context(headers: Mapping[str, str]) -> Any:
    """The W3C trace context of an incoming request, for the server span's parent."""
    return propagate.extract(headers)


@contextmanager
def upstream_span(url: str, headers: dict[str, str]) -> Iterator[trace.Span]:
    """The client span for one upstream call; injects traceparent into ``headers``.

    The headers dict is reused for every retry of the call, so the injection
    happens once here, inside the span context.
    """
    span = tracer().start_span(
        UPSTREAM_SPAN, kind=SpanKind.CLIENT, attributes={"upstream.url": url}
    )
    with trace.use_span(span, end_on_exit=True):
        propagate.inject(headers)
        yield span


def record_request_outcome(span: trace.Span, attributes: Mapping[str, Any]) -> None:
    """Set outcome attributes (status, tokens, cost) on the request's server span."""
    if span.is_recording():
        span.set_attributes(dict(attributes))
