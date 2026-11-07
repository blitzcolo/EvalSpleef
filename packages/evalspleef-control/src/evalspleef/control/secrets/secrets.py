"""Secret resolution errors.

Messages name the reference (a lookup path, not a value) and never contain
secret material (PRD FR-ABS security rules; TECH-DESIGN section 13).
"""

from __future__ import annotations


class SecretResolutionError(KeyError):
    """A reference could not be resolved to a value."""


class UnsupportedSchemeError(SecretResolutionError):
    """No registered resolver accepts the reference's scheme."""
