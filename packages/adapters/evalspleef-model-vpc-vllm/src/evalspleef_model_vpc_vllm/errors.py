"""Typed errors of the vpc-vllm provider.

Refines the SPI taxonomy so callers can handle EndpointMismatch and EndpointUnavailable
while receiving distinct, actionable error classifications.
"""

from __future__ import annotations

from evalspleef.spi.errors import EndpointMismatch, SpleefError


class CredentialRejected(EndpointMismatch):
    """HTTP 401: credentials missing, invalid or revoked."""


class AccessForbidden(EndpointMismatch):
    """HTTP 403: permission denied or account does not own the resource."""


class DeploymentNotFound(EndpointMismatch):
    """HTTP 404: the resource, base URL or model was not found."""


class RequestRejected(EndpointMismatch):
    """HTTP 400/422: the endpoint understood the request and refused it."""


class OrchestrationError(SpleefError):
    """Failure during container or instance orchestration."""

    error_class = "environment"
    disposition = "retry"
    retryable = True


class KubeApiError(OrchestrationError):
    """Kubernetes API operation failure."""


class GpuInstanceError(OrchestrationError):
    """GPU instance API operation failure."""


class PlanInputMissing(SpleefError):
    """Deployment plan cannot be determined due to missing parameters."""

    disposition = "manual"
