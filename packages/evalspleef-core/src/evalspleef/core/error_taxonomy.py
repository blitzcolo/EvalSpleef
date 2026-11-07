"""Provider exception taxonomy (TECH-DESIGN section 5.3, PRD FR-FLOW-05).

The core package must not import ``evalspleef.spi``, so the mapping is keyed by
the SPI exception class *name* and resolved by the caller that catches the
exception.  Values are ``(error_class, disposition)``:

* ``error_class`` is one of the PRD appendix A.2 section 4.6 classes, or
  ``None`` when TECH-DESIGN deliberately leaves it unset because the failure
  should have been caught at freeze time.
* ``disposition`` is one of ``retry`` / ``score`` / ``zero`` / ``manual`` /
  ``discard``, or ``None`` when no disposition applies.

PRD FR-FLOW-05 additions are covered by the same rows: endpoint failures map to
``provider``, sandbox/host/quota failures map to ``environment``, and a probe
that does not match runtime behaviour surfaces as ``EndpointMismatch``.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["ERROR_TAXONOMY", "TAXONOMY_ENTRY", "taxonomy_for"]

#: ``(error_class, disposition)`` pair; see TECH-DESIGN section 5.3.
TAXONOMY_ENTRY = tuple[str | None, str | None]

#: SPI exception class name -> ``(error_class, disposition)``.
ERROR_TAXONOMY: Mapping[str, TAXONOMY_ENTRY] = {
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


def taxonomy_for(class_name: str) -> TAXONOMY_ENTRY:
    """Return the taxonomy row for an SPI exception class name.

    Raises:
        KeyError: The class name is not part of the taxonomy.
    """
    return ERROR_TAXONOMY[class_name]
