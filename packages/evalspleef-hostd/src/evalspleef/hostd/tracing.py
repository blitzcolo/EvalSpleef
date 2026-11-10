"""OpenTelemetry spans for the Hostd gRPC service and its RemoteProvider client.

The daemon's server interceptor starts one server span per RPC, names it with the
full method path, records the gRPC status and extracts the W3C trace context the
RemoteProvider client interceptor injected, so a control-plane call and the hostd
handling it share one trace. The service decorators add the sandbox id when the
request carries one.

Runners are not involved here: hostd never sees model traffic, only sandbox RPCs.
"""

# mypy: disable-error-code="import-untyped,no-untyped-call"
from __future__ import annotations

from typing import Any

import grpc
from opentelemetry.instrumentation.grpc import (
    aio_client_interceptors,
    aio_server_interceptor,
)

SERVICE_NAME = "spleef-hostd"


def server_interceptor() -> grpc.aio.ServerInterceptor:
    """One server span per Hostd RPC (method, gRPC status); extracts trace context."""
    return aio_server_interceptor()


def channel_interceptors() -> list[grpc.aio.ClientInterceptor]:
    """Client spans for a RemoteProvider channel; injects traceparent into RPC metadata."""
    interceptors: list[grpc.aio.ClientInterceptor] = aio_client_interceptors()
    return interceptors


def request_attributes(request: Any) -> dict[str, str]:
    """Span attributes for an RPC request: the sandbox id, when it names one."""
    sandbox_id = getattr(request, "sandbox_id", "")
    return {"sandbox_id": sandbox_id} if isinstance(sandbox_id, str) and sandbox_id else {}
