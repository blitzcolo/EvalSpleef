"""Conformance case contract (TECH-DESIGN section 15.1).

A case is the smallest unit of the conformance kit: one behaviour of one provider
SPI checked against one adapter. Case modules expose ``CASES`` and are imported by
the pytest plugin at collection time (see ``plugin.CASE_MODULES``).

Applicability rules (FR-ABS-03):

* ``applies`` returns ``None`` when the case applies to the target and a short
  reason string when it does not. A case that needs a capability the target does
  not declare MUST report not-applicable through ``applies``; it must never be
  silently passed.
* A capability the target declares but whose case cannot run (for example a
  missing fault injector) is a failure: the case raises ``AssertionError`` from
  ``run`` with a clear message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from evalspleef.conformance.target import ConformanceTarget


@dataclass(frozen=True)
class Case:
    """One conformance check against a provider adapter.

    ``applies`` returns ``None`` when the case applies to the target and a short
    reason string when it does not. ``run`` returns ``None`` on pass and raises
    ``AssertionError`` with a clear message on failure.
    """

    id: str
    """Stable case id, for example ``C-LIFE-01`` or ``M-API-01``."""

    kind: Literal["sandbox", "model"]
    """Which provider SPI this case checks."""

    title: str
    """Human-readable one-line description."""

    applies: Callable[[ConformanceTarget], str | None]
    """Return ``None`` when applicable to the target, else a short reason."""

    run: Callable[[ConformanceTarget], Awaitable[None]]
    """Raise ``AssertionError`` with a clear message on failure; return ``None`` on pass."""
