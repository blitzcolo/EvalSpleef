"""Typed provider errors and their mapping to the platform error taxonomy.

Command timeouts are not errors: they are reported as ``ExecResult.timed_out`` and handled
by the caller as model behaviour (PRD appendix B.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Literal

ErrorClassName = Literal[
    "provider", "model", "cheat", "isolation", "environment", "verification", "judge", "version"
]
DispositionName = Literal["retry", "score", "zero", "manual", "discard"]


class SpleefError(Exception):
    """Base class. ``error_class`` and ``disposition`` follow TECH-DESIGN section 5.3."""

    error_class: ClassVar[ErrorClassName | None] = None
    disposition: ClassVar[DispositionName | None] = None
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, detail: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: Mapping[str, object] = dict(detail or {})


class CapabilityNotSupported(SpleefError):
    """A request exceeds the provider's declared capabilities. Never caught to degrade silently."""

    disposition = "manual"


class PolicyNotEnforceable(SpleefError):
    """A network or resource policy cannot be enforced by this provider."""

    error_class = "environment"
    disposition = "manual"


class QuotaExceeded(SpleefError):
    """Vendor quota or host slots exhausted; the caller queues and retries."""

    error_class = "environment"
    disposition = "retry"
    retryable = True


class SandboxCreateError(SpleefError):
    """Sandbox creation failed (image, template, host). Retried up to three times on another instance."""

    error_class = "environment"
    disposition = "retry"
    retryable = True


class SandboxLost(SpleefError):
    """The instance vanished mid-trial (host offline, TTL expiry, vendor reclaim)."""

    error_class = "environment"
    disposition = "discard"


class TransferError(SpleefError):
    """Upload or download failed."""

    error_class = "environment"
    disposition = "retry"
    retryable = True


class EndpointUnavailable(SpleefError):
    """Endpoint unreachable, cold-start timeout, or persistent 5xx."""

    error_class = "provider"
    disposition = "retry"
    retryable = True


class EndpointMismatch(SpleefError):
    """The responding model or vendor differs from what was registered."""

    error_class = "provider"
    disposition = "discard"


class ProbeFailed(SpleefError):
    """Capability probing failed; only raised at freeze time, never during a run."""
