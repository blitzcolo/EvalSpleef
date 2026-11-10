"""Judge sandboxes as leases: role ``judge``, the trial's own template, network off.

Every judge sandbox is a recorded lease (``sandbox.provisioned``, ``teardown.*``), created
from the trial's template (the verifier's image, so the artifact's toolchain exists) with
the verifier's requirements, network off (loopback only) and the declared zones. The lease
manager destroys it when the sample's ``async with`` exits, whatever happened inside.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from evalspleef.spi.sandbox import Sandbox, SandboxCapabilities

from ..leases.manager import LeaseManager, LeaseRequest
from ..scheduling.matching import verifier_requirements
from .agent_bridge import JudgeSandboxRequest
from .agent_plan import judge_carrier

__all__ = ["LeaseJudgeOpener"]


class LeaseJudgeOpener:
    """A :class:`~.agent_bridge.JudgeSandboxOpener` backed by the lease manager."""

    def __init__(
        self,
        leases: LeaseManager,
        capabilities: Callable[[str], SandboxCapabilities],
        run_as_user: str = "agent",
    ) -> None:
        self._leases = leases
        self._capabilities = capabilities
        self._run_as_user = run_as_user

    @asynccontextmanager
    async def open(self, request: JudgeSandboxRequest) -> AsyncIterator[Sandbox]:
        trial = request.trial
        lease = LeaseRequest(
            batch_id=trial.batch_id,
            trial_id=trial.trial_id,
            role="judge",
            instance=judge_carrier(trial, self._capabilities),
            requirements=verifier_requirements(trial.requirements),
            template=trial.template,
            run_as_user=self._run_as_user,
            writable_paths=request.writable_paths,
        )
        async with self._leases.lease(lease) as leased:
            yield leased.sandbox
