"""Isolation profile: how a provider satisfies invariants I1-I16 (PRD appendix D.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .caps import Isolation


@dataclass(frozen=True)
class InvariantEntry:
    """How one invariant is met, how to check it, or why it is waived."""

    how: str
    probe: str | None = None
    waived: bool = False
    reason: str | None = None
    compensating: str | None = None
    approved_by: str | None = None
    expires: str | None = None


@dataclass(frozen=True)
class IsolationProfile:
    """Shipped with every sandbox adapter; the per-provider section of the isolation audit."""

    provider: str
    declared_isolation: Isolation
    verified_isolation: Isolation | None = None
    invariants: dict[str, InvariantEntry] = field(default_factory=dict)

    def waivers(self) -> dict[str, InvariantEntry]:
        return {key: entry for key, entry in self.invariants.items() if entry.waived}
