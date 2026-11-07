"""Contract between the batch driver and whatever runs a trial end to end.

The driver owns scheduling, leasing and bookkeeping; an executor owns everything that happens
inside a trial (preflight, canary, runner, observation, feedback rounds, verification, grading).
The driver hands it an already-leased agent sandbox and a way to open the verifier sandbox, and
turns the returned :class:`TrialOutcome` into trial rows, grade rows, events and the report.

Event ownership: the driver appends ``trial.created``, ``trial.leased``, ``sandbox.provisioned``,
``teardown.*``, ``trial.scored`` and ``trial.invalidated``. An executor appends the events of the
phases it runs (``preflight.result``, ``agent.step``, ``round.started`` and so on) and must not
append those five.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from evalspleef.spi.caps import RuntimeRequirements
from evalspleef.spi.sandbox import Sandbox, SandboxRole, TemplateRef

__all__ = [
    "LeaseHandle",
    "OutcomeStatus",
    "PlannedTrial",
    "TrialExecutor",
    "TrialLeases",
    "TrialOutcome",
]


@dataclass(frozen=True)
class PlannedTrial:
    """One trial attempt as planned at freeze. A retry is a new ``PlannedTrial``."""

    trial_id: str
    batch_id: str
    cell_id: str
    task_id: str
    model: str
    shell: str
    epoch: int
    index: int
    requirements: RuntimeRequirements
    template: TemplateRef
    agent_routes: tuple[str, ...]
    """Provider instances that can host the agent sandbox, in route order."""
    verifier_routes: tuple[str, ...]
    """Provider instances that can host the verifier sandbox (``network=off``)."""
    endpoint_id: str | None = None
    attempt: int = 0
    """Infra retries already spent on this epoch's chain; zero for the first attempt."""


@dataclass(frozen=True)
class LeaseHandle:
    """A live sandbox lease as seen by an executor. The driver destroys it, not the executor."""

    lease_id: str
    role: SandboxRole
    provider_instance: str
    host_id: str | None
    sandbox: Sandbox


class TrialLeases:
    """The sandboxes of one trial attempt.

    ``agent`` exists when the executor starts. ``open_verifier`` creates the verifier sandbox
    on demand, as a separate lease on a separate sandbox (invariant I10); calling it again
    returns the same handle. The driver destroys every lease when the executor returns, raises
    or is cancelled.
    """

    def __init__(self, agent: LeaseHandle, opener: Callable[[], Awaitable[LeaseHandle]]) -> None:
        self.agent = agent
        self._opener = opener
        self._verifier: LeaseHandle | None = None

    @property
    def verifier(self) -> LeaseHandle | None:
        """The verifier lease if :meth:`open_verifier` was called."""
        return self._verifier

    async def open_verifier(self) -> LeaseHandle:
        if self._verifier is None:
            self._verifier = await self._opener()
        return self._verifier


OutcomeStatus = Literal["scored", "invalid", "failed_infra", "paused_manual"]


@dataclass(frozen=True)
class TrialOutcome:
    """How a trial attempt ended.

    ``failed_infra`` makes the driver retry the trial as a new record (at most three times).
    ``record`` is the trial record JSON (``evalspleef.core.models.TrialRecord.to_dict``) and is
    required for ``scored``; its ``trial_id`` must equal the planned trial's id and its
    ``grades`` feed the grade table and the aggregate report.
    """

    status: OutcomeStatus
    record: Mapping[str, Any] | None = None
    reason: str = ""


class TrialExecutor(Protocol):
    """Runs one trial attempt against the leased sandboxes."""

    async def run_trial(self, trial: PlannedTrial, leases: TrialLeases) -> TrialOutcome:
        """Return the outcome, or raise a ``SpleefError``: errors that are retryable or
        environmental-and-discard become ``failed_infra``, all others ``invalid``."""
        ...
