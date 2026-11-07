"""Typed errors of the Novita providers.

All of them refine the SPI taxonomy so callers can keep catching ``EndpointMismatch`` or
``EndpointUnavailable`` while operators still get one distinct, actionable message per cause.
``detail["reason"]`` carries a stable machine-readable code.
"""

from __future__ import annotations

from evalspleef.spi.errors import EndpointMismatch, SpleefError


class InvalidBaseUrl(EndpointMismatch):
    """The pasted base_url is not an https URL on a ``*.novita.ai`` host."""


class ModelNameMismatch(EndpointMismatch):
    """The model name differs from the registration, or the endpoint answers as another model."""


class CredentialRejected(EndpointMismatch):
    """HTTP 401: the Novita API key is missing, wrong or revoked."""


class AccessForbidden(EndpointMismatch):
    """HTTP 403: the key is valid but does not belong to the deployment owner."""


class DeploymentNotFound(EndpointMismatch):
    """HTTP 404: the base_url or the model ID does not name a live deployment."""


class RequestRejected(EndpointMismatch):
    """HTTP 400/422: the endpoint understood the request and refused it."""


class PlanInputMissing(SpleefError):
    """The plan cannot be computed because the model sizing inputs were not supplied."""

    disposition = "manual"
