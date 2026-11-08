"""Errors and exit codes of the spleef CLI.

Exit codes (PRD FR-OPS-01): 0 success, 1 domain failure (a lint error, a
failed probe, a rejected model), 2 usage or configuration error (a malformed
config file, a missing gateway address).
"""

from __future__ import annotations

__all__ = ["CliError", "DomainError", "UsageError"]

EXIT_SUCCESS = 0
EXIT_DOMAIN = 1
EXIT_USAGE = 2


class CliError(Exception):
    """An expected failure that ends the command with a fixed exit code."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class DomainError(CliError):
    """The inputs were usable but the operation failed (exit 1)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_DOMAIN)


class UsageError(CliError):
    """Invocation or configuration problem (exit 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_USAGE)
