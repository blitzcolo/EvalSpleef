"""Provider error taxonomy (TECH-DESIGN 5.3, PRD FR-FLOW-05)."""

from __future__ import annotations

import pytest

from evalspleef.core.error_taxonomy import ERROR_TAXONOMY, taxonomy_for

EXPECTED = {
    "CapabilityNotSupported": (None, "manual"),
    "PolicyNotEnforceable": ("environment", "manual"),
    "QuotaExceeded": ("environment", "retry"),
    "SandboxCreateError": ("environment", "retry"),
    "SandboxLost": ("environment", "discard"),
    "TransferError": ("environment", "retry"),
    "EndpointUnavailable": ("provider", "retry"),
    "EndpointMismatch": ("provider", "discard"),
    "ProbeFailed": (None, None),
}


def test_taxonomy_matches_tech_design_5_3_exactly() -> None:
    assert dict(ERROR_TAXONOMY) == EXPECTED


def test_taxonomy_rows_use_frozen_spellings() -> None:
    error_classes = {
        "provider",
        "model",
        "cheat",
        "isolation",
        "environment",
        "verification",
        "judge",
        "version",
    }
    dispositions = {"retry", "score", "zero", "manual", "discard"}
    for error_class, disposition in ERROR_TAXONOMY.values():
        assert error_class is None or error_class in error_classes
        assert disposition is None or disposition in dispositions


def test_taxonomy_lookup() -> None:
    assert taxonomy_for("QuotaExceeded") == ("environment", "retry")
    with pytest.raises(KeyError):
        taxonomy_for("NotATypedError")


def test_taxonomy_is_plain_data_not_classes() -> None:
    assert all(isinstance(key, str) for key in ERROR_TAXONOMY)
    assert all(isinstance(value, tuple) and len(value) == 2 for value in ERROR_TAXONOMY.values())
