"""OpenTelemetry tracing for SPI calls (TECH-DESIGN section 12).

Every provider operation is one span with attributes ``trial_id``,
``provider_instance``, ``op``, ``bytes``, ``exit_code`` and ``timed_out``.
:func:`tracer` is a no-op until an SDK provider is configured.
"""

from .otlp import configure_tracing, flush_tracing
from .tracing import (
    TracedEndpointProvider,
    TracedSandbox,
    TracedSandboxProvider,
    traced_provider,
    tracer,
)

__all__ = [
    "TracedEndpointProvider",
    "TracedSandbox",
    "TracedSandboxProvider",
    "configure_tracing",
    "flush_tracing",
    "traced_provider",
    "tracer",
]
