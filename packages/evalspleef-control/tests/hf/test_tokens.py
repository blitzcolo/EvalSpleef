"""Tests for the HF token expiry gate (FR-MOD-02 item 5)."""

from __future__ import annotations

from datetime import date

from evalspleef.control.hf import check_token_expiry

TODAY = date(2026, 11, 7)
BATCH_END = date(2026, 11, 21)


def test_missing_expiry_blocks_with_warning() -> None:
    report = check_token_expiry(expires_at=None, today=TODAY, batch_end=BATCH_END)
    assert not report.ok
    assert report.warning
    assert report.days_remaining is None
    assert "no token expiry" in report.reason


def test_expired_token_fails() -> None:
    report = check_token_expiry(expires_at=date(2026, 11, 6), today=TODAY, batch_end=BATCH_END)
    assert not report.ok
    assert not report.warning
    assert report.days_remaining == -1
    assert "expired" in report.reason


def test_token_not_covering_batch_end_plus_margin_fails() -> None:
    # batch end 11-21 + 7 days = 11-28; expiring on 11-28 exactly is not enough
    report = check_token_expiry(expires_at=date(2026, 11, 28), today=TODAY, batch_end=BATCH_END)
    assert not report.ok
    assert report.warning
    assert "margin" in report.reason


def test_token_covering_batch_passes() -> None:
    report = check_token_expiry(expires_at=date(2027, 2, 1), today=TODAY, batch_end=BATCH_END)
    assert report.ok
    assert not report.warning
    assert report.days_remaining == 86


def test_token_within_warning_window_passes_with_warning() -> None:
    # covers the batch end (11-10 + 7 days) but expires within 14 days of today
    report = check_token_expiry(
        expires_at=date(2026, 11, 20), today=TODAY, batch_end=date(2026, 11, 10)
    )
    assert report.ok
    assert report.warning
    assert report.days_remaining == 13
    assert "13 days" in report.reason


def test_defaults_match_TECH_DESIGN() -> None:
    # 7-day margin: expiring exactly at batch end is not enough
    assert not check_token_expiry(
        expires_at=date(2026, 11, 21), today=TODAY, batch_end=BATCH_END
    ).ok
    # expiring one day after the margin passes
    assert check_token_expiry(expires_at=date(2026, 11, 29), today=TODAY, batch_end=BATCH_END).ok
