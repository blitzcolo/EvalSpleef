"""Retry of ``failed_infra`` trials as new trial records (PRD appendix B.6, docs/02 section 8).

A retry never reuses the failed record: it gets a new trial id with the same epoch and an
incremented index, and the old record stays ``invalid``. At most
:data:`~evalspleef.core.state.MAX_INFRA_RETRIES` retries are made per chain.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from evalspleef.core.models import format_trial_id, parse_trial_id
from evalspleef.core.state import MAX_INFRA_RETRIES

__all__ = ["MAX_INFRA_RETRIES", "RetryDecision", "next_trial_id", "retry_decision"]


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


def retry_decision(
    trial_id: str, attempt: int, *, max_retries: int = MAX_INFRA_RETRIES
) -> RetryDecision:
    """Decide what follows a ``failed_infra`` trial that has already used ``attempt`` retries."""
    if attempt >= max_retries:
        return RetryDecision(False, None, attempt, f"infra retries exhausted ({max_retries})")
    return RetryDecision(True, next_trial_id(trial_id), attempt + 1, "failed_infra")
