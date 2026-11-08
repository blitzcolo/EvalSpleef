"""Typed errors of the Beam provider.

They refine the SPI taxonomy so callers keep catching ``EndpointMismatch`` or
``EndpointUnavailable`` while operators get one distinct message per cause.
``detail["reason"]`` carries a stable machine-readable code.
"""

from __future__ import annotations

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable, SpleefError


class CredentialRejected(EndpointMismatch):
    """HTTP 401: the Beam token is missing, wrong or revoked."""


class AccessForbidden(EndpointMismatch):
    """HTTP 403: the token is valid but may not act on this deployment."""


class DeploymentNotFound(EndpointMismatch):
    """HTTP 404 or a gateway 'not found': the deployment does not exist (any more)."""


class RequestRejected(EndpointMismatch):
    """HTTP 400/422 or a gateway ``ok: false``: Beam understood the request and refused it."""


class LaunchFailed(EndpointUnavailable):
    """The launcher could not deploy the Pod (no capacity, image build, SDK failure, timeout)."""


class ParallelismMismatch(SpleefError):
    """The parallel layout of the engine arguments does not fit the requested GPU count."""

    disposition = "manual"


class PlanInputMissing(SpleefError):
    """The plan cannot be computed because a required deployment parameter is missing."""

    disposition = "manual"
