"""OpenTelemetry spans for agent runners.

Runners execute inside the control-plane process (the unified shell loop lives
next to the ``spleef`` CLI; native shells drive a CLI inside the sandbox from
the same process), so runner spans carry that process's ``service.name`` and
identify themselves with ``runner``/``trial_id`` attributes instead. Model
calls of the unified runner send the W3C traceparent to the gateway, which
parents the gateway's server span to the runner's trace.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from evalspleef.spi.sandbox import Sandbox
from evalspleef.telemetry import get_tracer

TRACER_NAME = "evalspleef.runners"

CLIENT = SpanKind.CLIENT


def tracer() -> trace.Tracer:
    """The runner tracer; no-op until an SDK provider is configured."""
    return get_tracer(TRACER_NAME)


def trial_id(sandbox: Sandbox | None) -> str | None:
    """The trial id a runner's sandbox belongs to, when known."""
    spec = getattr(sandbox, "spec", None)
    value = getattr(spec, "trial_id", None)
    return value if isinstance(value, str) and value else None


def turn_attributes(runner_name: str, sandbox: Sandbox | None) -> dict[str, Any]:
    """Attributes every runner span carries."""
    attributes: dict[str, Any] = {"runner": runner_name}
    current = trial_id(sandbox)
    if current is not None:
        attributes["trial_id"] = current
    return attributes


def inject_traceparent(headers: dict[str, str]) -> None:
    """Write the active W3C trace context into outbound request headers."""
    propagate.inject(headers)
