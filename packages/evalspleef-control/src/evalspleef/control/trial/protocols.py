"""Interfaces the trial runner depends on; other engineers implement them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from evalspleef.core.feedback import FeedbackDecision, LeakViolation
from evalspleef.spi.runner import AgentRunner
from evalspleef.spi.sandbox import SandboxProvider

from ..batch.executor import LeaseHandle, PlannedTrial
from .types import EvaluationResult, TrialContext, TrialSetup

__all__ = [
    "AgentSandboxReplacer",
    "Clock",
    "Evaluator",
    "FeedbackWriter",
    "ProviderDirectory",
    "RunnerFactory",
    "TrialSetupResolver",
]


class Clock(Protocol):
    """Epoch-seconds clock with an awaitable sleep, so tests drive time by hand."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class Evaluator(Protocol):
    """Observes the agent's work (``final=False``) and verifies it (``final=True``).

    A non-final call observes the agent's work after each round; how it does so (visible checks
    in the agent sandbox, hidden tests in the verifier sandbox) is the evaluator's business.
    The final call is the authoritative verification: it must use a separate verifier sandbox
    (``ctx.leases.open_verifier()``, invariant I10), upload only the declared artifacts and the
    hidden tests, and report every hard gate of the pack. Items that must never reach the
    simulated user are reported by the final call only. If the final call contradicts the last
    observation without a budget, feedback or abort reason, the trial goes to manual review.
    Raise a ``SpleefError`` for infrastructure problems; the runner maps it.
    """

    async def evaluate(self, ctx: TrialContext, round_no: int, final: bool) -> EvaluationResult: ...


class ProviderDirectory(Protocol):
    """Finds the provider behind a lease; ``ProviderRegistry`` satisfies it."""

    def sandbox_instance(self, name: str) -> SandboxProvider: ...


class AgentSandboxReplacer(Protocol):
    """Gives the trial a fresh agent sandbox after an isolation preflight failure (B.2a, S5).

    ``TrialLeases`` carries no way to lease a second agent sandbox, so the batch wiring supplies
    this. The returned sandbox lives on an instance outside ``exclude`` and is owned by the
    implementation, which destroys it when the trial ends.
    """

    async def replace(
        self, trial: PlannedTrial, failed: LeaseHandle, *, exclude: frozenset[str]
    ) -> LeaseHandle: ...


class TrialSetupResolver(Protocol):
    """Resolves the task pack and the model endpoint of a planned trial."""

    def __call__(self, trial: PlannedTrial) -> TrialSetup: ...


class RunnerFactory(Protocol):
    """Creates a fresh agent runner for one trial."""

    def __call__(self, trial: PlannedTrial) -> AgentRunner: ...


class FeedbackWriter(Protocol):
    """Produces the words of the simulated user. Never sees rubric ids, hidden tests or paths."""

    def compose(self, decision: FeedbackDecision) -> str:
        """The F1-F3 message for the decision's defect; empty when no material exists."""
        ...

    def rewrite(self, text: str, violations: Sequence[LeakViolation]) -> str:
        """A new version of ``text`` without what the leak checker flagged."""
        ...

    def nudge(self, index: int) -> str:
        """An F0 message without information, at most 60 characters."""
        ...

    def answer(self, question: str) -> str:
        """The persona's FAQ answer to a clarification question."""
        ...
