"""Map upstream HTTP errors onto the Anthropic and OpenAI error envelopes.

The gateway receives an error status, code and message from the upstream endpoint (or
produces one itself for budget enforcement) and must answer the calling client in the
protocol it speaks. The Anthropic envelope has no code field, so the budget stop reasons
``cost_cap`` and ``token_cap`` are carried as a message prefix; the OpenAI envelope keeps
them in ``error.code`` unchanged.
"""

from __future__ import annotations

from typing import Any

from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP

BUDGET_CODES: frozenset[str] = frozenset({COST_CAP, TOKEN_CAP})

_DEFAULT_MESSAGE = "upstream error"


def _is_overloaded(status: int, code: str | None) -> bool:
    if status == 529:
        return True
    return status == 503 and code is not None and "overload" in code.lower()


def anthropic_error_type(status: int, code: str | None = None) -> str:
    """Return the Anthropic error type for an upstream status and code."""
    if code in BUDGET_CODES:
        return "rate_limit_error"
    if status in (400, 404, 413, 422):
        return "invalid_request_error"
    if status in (401, 403):
        return "authentication_error"
    if status == 429:
        return "rate_limit_error"
    if _is_overloaded(status, code):
        return "overloaded_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


def openai_error_type(status: int, code: str | None = None) -> str:
    """Return the OpenAI error type for an upstream status and code."""
    if code in BUDGET_CODES:
        return "rate_limit_error"
    if status in (400, 404, 413, 422):
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 429:
        return "rate_limit_error"
    if _is_overloaded(status, code):
        return "server_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


def anthropic_error_envelope(
    status: int, code: str | None = None, message: str = ""
) -> dict[str, Any]:
    """Build an Anthropic ``{"type": "error", "error": {...}}`` envelope.

    A budget code is prefixed to the message because the Anthropic error object has no
    ``code`` field.
    """
    text = message or _DEFAULT_MESSAGE
    if code in BUDGET_CODES and not text.startswith(f"{code}:"):
        text = f"{code}: {text}"
    return {
        "type": "error",
        "error": {"type": anthropic_error_type(status, code), "message": text},
    }


def openai_error_envelope(
    status: int, code: str | None = None, message: str = ""
) -> dict[str, Any]:
    """Build an OpenAI ``{"error": {...}}`` envelope, preserving budget codes verbatim."""
    return {
        "error": {
            "message": message or _DEFAULT_MESSAGE,
            "type": openai_error_type(status, code),
            "param": None,
            "code": code,
        }
    }
