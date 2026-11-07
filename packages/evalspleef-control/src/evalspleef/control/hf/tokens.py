"""Token expiry checks for HF identities (FR-MOD-02 item 5, freeze-time gate)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

__all__ = ["TokenExpiryReport", "check_token_expiry"]


@dataclass(frozen=True)
class TokenExpiryReport:
    """Outcome of the freeze-time token validity check.

    ``ok`` means the token covers the whole batch plus the required margin;
    ``warning`` asks for attention without blocking (expiry recorded but
    close, or the batch cannot be fully covered by a still-valid token).
    """

    ok: bool
    warning: bool
    days_remaining: int | None
    reason: str


def check_token_expiry(
    *,
    expires_at: date | None,
    today: date,
    batch_end: date,
    warn_within_days: int = 14,
    required_margin_days: int = 7,
) -> TokenExpiryReport:
    """Check an HF token's expiry against the batch window.

    Freeze-time rule (TECH-DESIGN section 6.5): the token must expire later
    than ``batch_end + 7 days``; a token expiring within 14 days raises a
    warning. A token without a recorded expiry cannot be verified and blocks
    freezing. ``today`` and ``batch_end`` are injected so tests stay
    deterministic; grading-style purity is not required here (this is a
    control-plane gate, not grading).
    """
    if expires_at is None:
        return TokenExpiryReport(
            ok=False,
            warning=True,
            days_remaining=None,
            reason="no token expiry date is recorded; cannot verify the batch is covered",
        )
    days_remaining = (expires_at - today).days
    if expires_at <= today:
        return TokenExpiryReport(
            ok=False,
            warning=False,
            days_remaining=days_remaining,
            reason=f"token expired on {expires_at.isoformat()}",
        )
    required_until = batch_end + timedelta(days=required_margin_days)
    if expires_at <= required_until:
        return TokenExpiryReport(
            ok=False,
            warning=True,
            days_remaining=days_remaining,
            reason=(
                f"token expires {expires_at.isoformat()}, which does not cover the batch "
                f"end ({batch_end.isoformat()}) plus the {required_margin_days}-day margin"
            ),
        )
    if days_remaining <= warn_within_days:
        return TokenExpiryReport(
            ok=True,
            warning=True,
            days_remaining=days_remaining,
            reason=(
                f"token covers the batch but expires in {days_remaining} days "
                f"(within the {warn_within_days}-day warning window)"
            ),
        )
    return TokenExpiryReport(
        ok=True,
        warning=False,
        days_remaining=days_remaining,
        reason=f"token covers the batch with {days_remaining} days to spare",
    )
