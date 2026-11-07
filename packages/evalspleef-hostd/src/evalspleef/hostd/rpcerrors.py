"""Mapping between typed SPI errors and gRPC statuses.

The daemon sends the exact error class name in trailing metadata, so the RemoteProvider can
raise the same typed error the local provider raised. The status code is only a fallback for
peers that did not attach the metadata.
"""

# mypy: disable-error-code="import-untyped"
from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import grpc

from evalspleef.spi.errors import (
    CapabilityNotSupported,
    EndpointUnavailable,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
    TransferError,
)

ERROR_METADATA_KEY = "x-spleef-error"


class HostdAuthError(SpleefError):
    """The peer is not authenticated or not authorised (token, certificate, identity)."""

    error_class = "environment"
    disposition = "manual"


_BY_NAME: dict[str, type[SpleefError]] = {
    cls.__name__: cls
    for cls in (
        CapabilityNotSupported,
        PolicyNotEnforceable,
        QuotaExceeded,
        SandboxCreateError,
        SandboxLost,
        TransferError,
        EndpointUnavailable,
        HostdAuthError,
    )
}

_STATUS: dict[type[SpleefError], grpc.StatusCode] = {
    CapabilityNotSupported: grpc.StatusCode.UNIMPLEMENTED,
    PolicyNotEnforceable: grpc.StatusCode.FAILED_PRECONDITION,
    QuotaExceeded: grpc.StatusCode.RESOURCE_EXHAUSTED,
    SandboxCreateError: grpc.StatusCode.INTERNAL,
    SandboxLost: grpc.StatusCode.NOT_FOUND,
    TransferError: grpc.StatusCode.DATA_LOSS,
    EndpointUnavailable: grpc.StatusCode.UNAVAILABLE,
    HostdAuthError: grpc.StatusCode.PERMISSION_DENIED,
}

_FALLBACK_BY_STATUS: dict[grpc.StatusCode, type[SpleefError]] = {
    grpc.StatusCode.UNIMPLEMENTED: CapabilityNotSupported,
    grpc.StatusCode.FAILED_PRECONDITION: PolicyNotEnforceable,
    grpc.StatusCode.RESOURCE_EXHAUSTED: QuotaExceeded,
    grpc.StatusCode.UNAUTHENTICATED: HostdAuthError,
    grpc.StatusCode.PERMISSION_DENIED: HostdAuthError,
    grpc.StatusCode.DATA_LOSS: TransferError,
}


def status_for(error: SpleefError) -> grpc.StatusCode:
    for cls in type(error).__mro__:
        if cls in _STATUS:
            return _STATUS[cls]
    return grpc.StatusCode.INTERNAL


def error_name(error: SpleefError) -> str:
    for cls in type(error).__mro__:
        if cls.__name__ in _BY_NAME:
            return cls.__name__
    return SandboxCreateError.__name__


async def abort_with(context: Any, error: SpleefError) -> None:
    """Terminate the RPC carrying the typed error; never returns."""
    await context.abort(
        status_for(error),
        error.message or type(error).__name__,
        ((ERROR_METADATA_KEY, error_name(error)),),
    )


def translate(
    exc: grpc.aio.AioRpcError, *, fallback: type[SpleefError] = SandboxLost
) -> SpleefError:
    """Turn a failed RPC into the typed SPI error.

    A broken connection (UNAVAILABLE, CANCELLED, DEADLINE_EXCEEDED) means the host or the
    stream is gone: callers get ``fallback``, which is SandboxLost for calls on a live sandbox.
    """
    metadata: Mapping[str, str] = {
        str(key): str(value) for key, value in (exc.trailing_metadata() or ())
    }
    message = exc.details() or exc.code().name
    named = _BY_NAME.get(metadata.get(ERROR_METADATA_KEY, ""))
    if named is not None:
        return named(message)
    by_status = _FALLBACK_BY_STATUS.get(exc.code())
    if by_status is not None:
        return by_status(message)
    return fallback(f"host connection failed: {exc.code().name}: {message}")


Handler = Callable[[Any, Any, Any], Awaitable[Any]]


def unary_guard(handler: Handler) -> Handler:
    """Run a unary or request-streaming handler, aborting the RPC on a typed SPI error."""

    @functools.wraps(handler)
    async def wrapper(self: Any, request: Any, context: Any) -> Any:
        try:
            return await handler(self, request, context)
        except SpleefError as exc:
            await abort_with(context, exc)

    return wrapper


StreamHandler = Callable[[Any, Any, Any], AsyncIterator[Any]]


def stream_guard(handler: StreamHandler) -> StreamHandler:
    """Like ``unary_guard`` for response-streaming handlers."""

    @functools.wraps(handler)
    async def wrapper(self: Any, request: Any, context: Any) -> AsyncIterator[Any]:
        try:
            async for item in handler(self, request, context):
                yield item
        except SpleefError as exc:
            await abort_with(context, exc)

    return wrapper
