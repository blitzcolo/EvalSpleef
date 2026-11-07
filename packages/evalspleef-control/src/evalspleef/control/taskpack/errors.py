"""Task-pack errors."""

from __future__ import annotations

from evalspleef.spi.errors import SpleefError


class TaskPackError(SpleefError):
    """A task pack cannot be loaded or violates the pack contract."""

    error_class = "version"
    disposition = "manual"
