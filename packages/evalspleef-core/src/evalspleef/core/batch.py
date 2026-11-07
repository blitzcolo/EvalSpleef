"""Batch lifecycle machine (PRD appendix B.1, PRD FR-FLOW-01/02).

A batch walks Draft -> Frozen -> Running -> Graded -> Audited -> Published ->
Archived.  Two of those moves carry guards that the caller supplies as data:

* Draft -> Frozen freezes the plan and the version matrix (PRD FR-FLOW-01):
  the task-pack lint must be green, the budget approved, image digests pinned,
  and the endpoint probe report ids, model revisions, template digest map and
  gateway version must all be frozen.
* Any move into Running requires the endpoint and carrier checks of PRD
  FR-FLOW-02: self-deployed endpoints ``ready + warm``, carrier capacity for
  the planned concurrency and duration, and valid isolation profiles.

An edge outside the table raises :class:`IllegalBatchTransition`; a legal edge
whose guards are not satisfied raises :class:`BatchGuardFailed` naming every
failed condition.  A successful move returns a plain dict ready to be placed
in an :class:`evalspleef.core.events.EventEnvelope` payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "TRANSITIONS",
    "BatchGuardFailed",
    "BatchMachine",
    "BatchState",
    "FreezeGuards",
    "GuardFailure",
    "IllegalBatchTransition",
    "RunGuards",
]


class BatchState(StrEnum):
    """Batch lifecycle states (PRD appendix B.1)."""

    DRAFT = "draft"
    FROZEN = "frozen"
    RUNNING = "running"
    GRADED = "graded"
    AUDITED = "audited"
    PUBLISHED = "published"
    ARCHIVED = "archived"


#: Lifecycle order, used only to make tuple results deterministic.
_ORDER: tuple[BatchState, ...] = (
    BatchState.DRAFT,
    BatchState.FROZEN,
    BatchState.RUNNING,
    BatchState.GRADED,
    BatchState.AUDITED,
    BatchState.PUBLISHED,
    BatchState.ARCHIVED,
)

#: Legal edges.  ``Audited -> Running`` is the "audit found a problem, rerun"
#: failure disposition of PRD appendix B.1.
TRANSITIONS: Mapping[BatchState, frozenset[BatchState]] = {
    BatchState.DRAFT: frozenset({BatchState.FROZEN}),
    BatchState.FROZEN: frozenset({BatchState.RUNNING}),
    BatchState.RUNNING: frozenset({BatchState.GRADED}),
    BatchState.GRADED: frozenset({BatchState.AUDITED}),
    BatchState.AUDITED: frozenset({BatchState.PUBLISHED, BatchState.RUNNING}),
    BatchState.PUBLISHED: frozenset({BatchState.ARCHIVED}),
    BatchState.ARCHIVED: frozenset(),
}


class IllegalBatchTransition(ValueError):
    """Raised when the requested edge is not in the PRD appendix B.1 table."""

    def __init__(self, from_state: BatchState, to_state: BatchState | str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal batch transition: {from_state} -> {to_state}")


@dataclass(frozen=True)
class GuardFailure:
    """One unsatisfied guard condition and the caller-supplied reason."""

    condition: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"condition": self.condition, "reason": self.reason}


class BatchGuardFailed(ValueError):
    """Raised when a legal edge's guard conditions are not all satisfied."""

    def __init__(
        self,
        from_state: BatchState,
        to_state: BatchState,
        failures: tuple[GuardFailure, ...],
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.failures = failures
        detail = "; ".join(f"{failure.condition} ({failure.reason})" for failure in failures)
        super().__init__(f"batch guard rejected {from_state} -> {to_state}: {detail}")

    @property
    def failed_conditions(self) -> tuple[str, ...]:
        """Names of the conditions that blocked the move, in declaration order."""
        return tuple(failure.condition for failure in self.failures)


def _failures(
    conditions: Mapping[str, bool], reasons: Mapping[str, str]
) -> tuple[GuardFailure, ...]:
    return tuple(
        GuardFailure(name, reasons.get(name, "condition not satisfied"))
        for name, satisfied in conditions.items()
        if not satisfied
    )


@dataclass(frozen=True)
class FreezeGuards:
    """Conditions for Draft -> Frozen (PRD appendix B.1 and FR-FLOW-01).

    Every condition defaults to ``False`` so an omitted guard blocks the freeze
    instead of silently allowing it.  ``reasons`` maps a condition name to a
    human-readable failure reason; unnamed failures get a generic reason.
    """

    lint_passed: bool = False
    budget_approved: bool = False
    image_digests_locked: bool = False
    endpoint_probe_reports_frozen: bool = False
    model_revisions_frozen: bool = False
    template_digest_map_frozen: bool = False
    gateway_version_frozen: bool = False
    reasons: Mapping[str, str] = field(default_factory=dict)

    def conditions(self) -> dict[str, bool]:
        """All conditions keyed by the name used in failure reports."""
        return {
            "lint_passed": self.lint_passed,
            "budget_approved": self.budget_approved,
            "image_digests_locked": self.image_digests_locked,
            "endpoint_probe_reports_frozen": self.endpoint_probe_reports_frozen,
            "model_revisions_frozen": self.model_revisions_frozen,
            "template_digest_map_frozen": self.template_digest_map_frozen,
            "gateway_version_frozen": self.gateway_version_frozen,
        }

    def failures(self) -> tuple[GuardFailure, ...]:
        return _failures(self.conditions(), self.reasons)

    @property
    def satisfied(self) -> bool:
        return not self.failures()


@dataclass(frozen=True)
class RunGuards:
    """Conditions for any move into Running (PRD FR-FLOW-02)."""

    endpoints_ready_warm: bool = False
    carrier_capacity_satisfiable: bool = False
    isolation_profiles_valid: bool = False
    reasons: Mapping[str, str] = field(default_factory=dict)

    def conditions(self) -> dict[str, bool]:
        return {
            "endpoints_ready_warm": self.endpoints_ready_warm,
            "carrier_capacity_satisfiable": self.carrier_capacity_satisfiable,
            "isolation_profiles_valid": self.isolation_profiles_valid,
        }

    def failures(self) -> tuple[GuardFailure, ...]:
        return _failures(self.conditions(), self.reasons)

    @property
    def satisfied(self) -> bool:
        return not self.failures()


class BatchMachine:
    """Mutable walker over the PRD appendix B.1 batch transition table."""

    def __init__(self, state: BatchState = BatchState.DRAFT) -> None:
        self._state = BatchState(state)

    @property
    def state(self) -> BatchState:
        """Current batch state."""
        return self._state

    def legal_targets(self) -> tuple[BatchState, ...]:
        """States reachable from the current state, in lifecycle order."""
        reachable = TRANSITIONS[self._state]
        return tuple(state for state in _ORDER if state in reachable)

    def can_move(self, target: BatchState) -> bool:
        """Whether ``target`` is a legal edge from the current state."""
        try:
            target_state = BatchState(target)
        except ValueError:
            return False
        return target_state in TRANSITIONS[self._state]

    def move(
        self,
        target: BatchState,
        guards: FreezeGuards | RunGuards | None = None,
        *,
        freeze_guards: FreezeGuards | None = None,
        run_guards: RunGuards | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply one transition and return an envelope-ready payload.

        Args:
            target: Destination state.
            guards: The guard object for this edge: :class:`FreezeGuards` for
                Draft -> Frozen, :class:`RunGuards` for any move into Running.
            freeze_guards: Keyword alternative for Draft -> Frozen.
            run_guards: Keyword alternative for moves into Running.
            reason: Optional rationale recorded in the payload.

        Raises:
            IllegalBatchTransition: The edge is not in the transition table.
            BatchGuardFailed: A guard condition of a legal edge is unsatisfied.
        """
        try:
            target_state = BatchState(target)
        except ValueError as exc:
            raise IllegalBatchTransition(self._state, target) from exc
        from_state = self._state
        if target_state not in TRANSITIONS[from_state]:
            raise IllegalBatchTransition(from_state, target_state)

        checked: tuple[str, ...] = ()
        if from_state == BatchState.DRAFT and target_state == BatchState.FROZEN:
            freeze = freeze_guards
            if freeze is None and isinstance(guards, FreezeGuards):
                freeze = guards
            freeze_gate = freeze if freeze is not None else FreezeGuards()
            failures = freeze_gate.failures()
            if failures:
                raise BatchGuardFailed(from_state, target_state, failures)
            checked = tuple(freeze_gate.conditions())
        elif target_state == BatchState.RUNNING:
            run = run_guards
            if run is None and isinstance(guards, RunGuards):
                run = guards
            run_gate = run if run is not None else RunGuards()
            failures = run_gate.failures()
            if failures:
                raise BatchGuardFailed(from_state, target_state, failures)
            checked = tuple(run_gate.conditions())

        self._state = target_state
        payload: dict[str, Any] = {
            "from_state": str(from_state),
            "to_state": str(target_state),
            "transition": f"{from_state}->{target_state}",
            "guards_checked": list(checked),
        }
        if reason is not None:
            payload["reason"] = reason
        return payload
