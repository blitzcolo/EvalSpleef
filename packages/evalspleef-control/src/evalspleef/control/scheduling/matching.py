"""Static matching at batch freeze (TECH-DESIGN section 7.2).

For every cell the planner resolves the routes of ``providers.yaml`` for the agent and the
verifier role, runs :func:`evalspleef.spi.matching.match_requirements` against each candidate
instance, and keeps the instances whose reason list is empty. A cell with no candidate for a
role is skipped with ``Skip(capability:<reasons>)``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from evalspleef.spi.caps import NetMode, OSFamily, RuntimeRequirements
from evalspleef.spi.matching import match_requirements
from evalspleef.spi.sandbox import SandboxCapabilities, SandboxRole

__all__ = [
    "CellMatch",
    "ProviderDirectory",
    "RoleMatch",
    "RouteLike",
    "StaticMatcher",
    "verifier_requirements",
]


class RouteLike(Protocol):
    """One entry of a resolved route (``evalspleef.control.providers.RoutePreference``)."""

    @property
    def instance(self) -> str: ...


class ProviderDirectory(Protocol):
    """The slice of ``ProviderRegistry`` that matching needs."""

    def route(
        self, *, os: OSFamily | str | None = None, role: str | None = None
    ) -> Sequence[RouteLike]: ...

    def capabilities(self, instance: str) -> SandboxCapabilities: ...

    def egress_proxy(self, instance: str) -> object | None: ...


@dataclass(frozen=True)
class RoleMatch:
    """Candidates and rejections for one sandbox role of a cell."""

    role: SandboxRole
    candidates: tuple[str, ...]
    """Instances whose capabilities satisfy the requirements, in route order."""
    rejections: Mapping[str, tuple[str, ...]]
    """Instance -> reasons it was rejected."""

    @property
    def reasons(self) -> tuple[str, ...]:
        """Distinct rejection reasons in first-seen order; ``route:none`` if nothing was routed."""
        seen: list[str] = []
        for reasons in self.rejections.values():
            for reason in reasons:
                if reason not in seen:
                    seen.append(reason)
        if not seen and not self.candidates:
            return ("route:none",)
        return tuple(seen)


@dataclass(frozen=True)
class CellMatch:
    """Result of matching one cell's requirements against the configured routes."""

    agent: RoleMatch
    verifier: RoleMatch

    @property
    def matched(self) -> bool:
        return bool(self.agent.candidates) and bool(self.verifier.candidates)

    @property
    def reasons(self) -> tuple[str, ...]:
        """Reasons of every role that has no candidate; empty when the cell matches."""
        reasons: list[str] = []
        for role_match in (self.agent, self.verifier):
            if role_match.candidates:
                continue
            for reason in role_match.reasons:
                if reason not in reasons:
                    reasons.append(reason)
        return tuple(reasons)


def verifier_requirements(requirements: RuntimeRequirements) -> RuntimeRequirements:
    """Verifier sandboxes run with ``network=off`` (TECH-DESIGN section 10)."""
    return replace(requirements, network=NetMode.OFF, allow_hosts=())


class StaticMatcher:
    """Matches cells to provider instances at freeze time."""

    def __init__(self, directory: ProviderDirectory) -> None:
        self._directory = directory

    def match_role(self, role: SandboxRole, requirements: RuntimeRequirements) -> RoleMatch:
        candidates: list[str] = []
        rejections: dict[str, tuple[str, ...]] = {}
        for preference in self._directory.route(os=requirements.os, role=role):
            instance = preference.instance
            if instance in candidates or instance in rejections:
                continue
            reasons = match_requirements(
                requirements,
                self._directory.capabilities(instance),
                egress_proxy_available=self._directory.egress_proxy(instance) is not None,
            )
            if reasons:
                rejections[instance] = tuple(reasons)
            else:
                candidates.append(instance)
        return RoleMatch(role, tuple(candidates), rejections)

    def match(self, requirements: RuntimeRequirements) -> CellMatch:
        return CellMatch(
            agent=self.match_role("agent", requirements),
            verifier=self.match_role("verifier", verifier_requirements(requirements)),
        )
