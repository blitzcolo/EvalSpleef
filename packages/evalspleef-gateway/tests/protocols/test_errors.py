"""Upstream error envelopes for both client protocols."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalspleef.gateway.protocols import (
    anthropic_error_envelope,
    openai_error_envelope,
)
from evalspleef.gateway.protocols.errors import BUDGET_CODES

FIXTURES = Path(__file__).parent / "fixtures"


def _cases() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(
        (FIXTURES / "errors.json").read_text(encoding="utf-8")
    )["cases"]
    return loaded


@pytest.mark.parametrize("case", _cases())
def test_error_envelopes(case: dict[str, Any]) -> None:
    assert (
        anthropic_error_envelope(case["status"], case["code"], case["message"]) == case["anthropic"]
    )
    assert openai_error_envelope(case["status"], case["code"], case["message"]) == case["openai"]


def test_budget_codes_always_surface_as_rate_limit() -> None:
    assert frozenset({"cost_cap", "token_cap"}) == BUDGET_CODES
    for code in BUDGET_CODES:
        anthropic = anthropic_error_envelope(400, code, "over budget")
        assert anthropic["error"]["type"] == "rate_limit_error"
        assert anthropic["error"]["message"] == f"{code}: over budget"
        openai = openai_error_envelope(400, code, "over budget")
        assert openai["error"]["type"] == "rate_limit_error"
        assert openai["error"]["code"] == code
