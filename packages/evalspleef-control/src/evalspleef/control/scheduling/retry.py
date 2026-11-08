"""Retry of ``failed_infra`` trials as new trial records (PRD appendix B.6, docs/02 section 8).

A retry never reuses the failed record: it gets a new trial id with the same epoch and an
incremented index, and the old record stays ``invalid``. Environment failures get at most
:data:`~evalspleef.core.state.MAX_INFRA_RETRIES` retries per chain. Provider failures (rate
limits, 5xx, dropped connections) get :data:`PROVIDER_RETRIES` retries with exponential
backoff; when those are spent the trial goes back to ``pending`` and runs again after the
rest of the batch, once (PRD appendix B.6).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from evalspleef.core.models import format_trial_id, parse_trial_id
from evalspleef.core.state import MAX_INFRA_RETRIES

__all__ = [
    "MAX_INFRA_RETRIES",
    "PROVIDER_BACKOFF_BASE_S",
    "PROVIDER_RETRIES",
    "RetryDecision",
    "next_trial_id",
    "retry_decision",
]

PROVIDER_RETRIES = 5
PROVIDER_BACKOFF_BASE_S = 2.0
"""Wait before the first provider retry; each later retry waits twice as long."""


def next_trial_id(trial_id: str) -> str:
    """Trial id of the retry: same task, model, shell and epoch, index plus one."""
    parts = parse_trial_id(trial_id)
    return format_trial_id(replace(parts, index=parts.index + 1))


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    next_trial_id: str | None
    attempt: int
    """Retries already spent on this chain, including this one when ``retry`` is true."""
    reason: str
    backoff_s: float = 0.0
    """Wait before the retry is queued."""
    defer: bool = False
    """Provider retries are spent: ``next_trial_id`` stays pending until the batch's other
    trials are done, then runs with a fresh retry budget."""


def retry_decision(
    trial_id: str,
    attempt: int,
    *,
    error_class: str | None = None,
    deferred: bool = False,
    max_retries: int = MAX_INFRA_RETRIES,
) -> RetryDecision:
    """Decide what follows a ``failed_infra`` trial that has already used ``attempt`` retries.

    ``deferred`` marks a chain that was already put back to pending once; it is not
    deferred again.
    """
    if error_class == "provider":
        if attempt < PROVIDER_RETRIES:
            backoff = PROVIDER_BACKOFF_BASE_S * 2**attempt
            return RetryDecision(True, next_trial_id(trial_id), attempt + 1, "provider", backoff)
        if deferred:
            return RetryDecision(
                False, None, attempt, f"provider retries exhausted again ({PROVIDER_RETRIES})"
            )
        return RetryDecision(
            False,
            next_trial_id(trial_id),
            attempt,
            f"provider retries exhausted ({PROVIDER_RETRIES}); deferred to the end of the batch",
            defer=True,
        )
    if attempt >= max_retries:
        return RetryDecision(False, None, attempt, f"infra retries exhausted ({max_retries})")
    return RetryDecision(True, next_trial_id(trial_id), attempt + 1, "failed_infra")
