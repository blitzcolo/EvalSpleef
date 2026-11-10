"""Errors and exit codes of the spleef CLI.

Exit codes (PRD FR-OPS-01): 0 success, 1 domain failure (a lint error, a
failed probe, a rejected model), 2 usage or configuration error (a malformed
config file, a missing gateway address), 3 not enough data (a calibration or
agreement command with nothing computable in the store).
"""

from __future__ import annotations

__all__ = ["CliError", "DomainError", "NoDataError", "UsageError"]

EXIT_SUCCESS = 0
EXIT_DOMAIN = 1
EXIT_USAGE = 2
#: Not enough data (a calibration/agreement command cannot compute the metric from the store),
#: distinct from 1, which means below the gate.
EXIT_NO_DATA = 3


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


class NoDataError(CliError):
    """The store holds too little data for the metric (exit 3, not a gate failure)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_NO_DATA)
