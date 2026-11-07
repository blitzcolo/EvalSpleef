"""Errors raised by the trial runner.

Each class declares ``error_class`` and ``disposition`` the way ``evalspleef.spi.errors`` does,
so the runner maps them to ``failed_infra`` or ``invalid`` with the same rule the batch driver
applies to any ``SpleefError`` (TECH-DESIGN section 5.3).
"""

from __future__ import annotations

from evalspleef.spi.errors import SpleefError

__all__ = [
    "CanaryInjectionError",
    "EvaluationTimeout",
    "IdentityHygieneError",
    "IsolationPreflightFailed",
    "RunnerCrashed",
]


class IsolationPreflightFailed(SpleefError):
    """An isolation probe (I1-I9) failed on this sandbox instance (PRD appendix B.2a, S5)."""

    error_class = "isolation"
    disposition = "retry"
    retryable = True


class CanaryInjectionError(SpleefError):
    """The canary could not be written or its placement hashes do not match (S3 fails: invalid)."""

    error_class = "isolation"
    disposition = "discard"


class IdentityHygieneError(SpleefError):
    """The sandbox carries evaluation identifiers or credentials (S4 fails: invalid)."""

    error_class = "isolation"
    disposition = "discard"


class RunnerCrashed(SpleefError):
    """The agent runner crashed again after the one allowed retry (PRD appendix B.3)."""

    error_class = "environment"
    disposition = "retry"
    retryable = True


class EvaluationTimeout(SpleefError):
    """One observation or verification exceeded ``max_observe_s`` (PRD appendix B.5)."""

    error_class = "verification"
    disposition = "retry"
    retryable = True
