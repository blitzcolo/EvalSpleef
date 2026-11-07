"""Secret references. Configuration holds references, never values."""

from __future__ import annotations

from typing import NewType, Protocol

SecretRef = NewType("SecretRef", str)
"""A reference such as ``vault://hf/eval-readonly`` or ``env://NOVITA_API_KEY``."""


class SecretResolver(Protocol):
    """Resolves a reference to its value at the moment of use. Implementations must not cache to disk."""

    def resolve(self, ref: SecretRef | str) -> str: ...
