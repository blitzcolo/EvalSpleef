"""Guard inputs for Draft -> Frozen and Frozen -> Running (PRD FR-FLOW-01, FR-FLOW-02, FR-MOD-06).

These functions only gather facts; :class:`evalspleef.core.batch.BatchMachine` decides, and
raises ``BatchGuardFailed`` naming every unsatisfied condition with the reason built here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Protocol

from evalspleef.core.batch import FreezeGuards, RunGuards
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.model import EndpointHandle, EndpointState, ModelEndpointProvider

from ..scheduling import InstanceCapacity, ResourceRequest
from .freeze import FreezeRecord
from .judge_panel import JudgeGuardFacts, judge_problems
from .plan import BatchPlan, PlannedCell

__all__ = [
    "INVARIANT_IDS",
    "EndpointStates",
    "ProviderEndpointStates",
    "freeze_guards",
    "run_guards",
]

#: Isolation invariants every provider profile must account for (PRD appendix D.2).
INVARIANT_IDS = tuple(f"I{number}" for number in range(1, 17))

_DIGEST_PREFIX = "sha256:"


class EndpointStates(Protocol):
    """Current lifecycle state of a model endpoint."""

    async def state(self, endpoint_id: str) -> EndpointState: ...


class ProviderEndpointStates:
    """:class:`EndpointStates` backed by ``ModelEndpointProvider.status`` of registered handles."""

    def __init__(self, entries: Mapping[str, tuple[ModelEndpointProvider, EndpointHandle]]) -> None:
        self._entries = entries

    async def state(self, endpoint_id: str) -> EndpointState:
        provider, handle = self._entries[endpoint_id]
        return await provider.status(handle)


def _is_digest(digest: str) -> bool:
    return digest.startswith(_DIGEST_PREFIX) and len(digest) > len(_DIGEST_PREFIX)


def freeze_guards(
    plan: BatchPlan,
    freeze: FreezeRecord,
    *,
    lint_passed: bool,
    budget_approved: bool,
    judge_facts: Mapping[str, JudgeGuardFacts] | None = None,
) -> FreezeGuards:
    """Draft -> Frozen: lint green, budget approved, digests locked, versions frozen.

    ``judge_facts`` carries the admission facts of the batch's judge panel (empty when
    the batch has no judges): a judge that is not registered, does not serve traffic
    or whose latest probe report fails a judge-required probe blocks the freeze, so a
    batch never starts trials whose judge calls would 404 on a deleted gateway route.
    """
    cells = plan.runnable_cells
    reasons: dict[str, str] = {}

    def settle(name: str, problems: list[str]) -> bool:
        if problems:
            reasons[name] = "; ".join(problems)
        return not problems

    budget_problems: list[str] = []
    if not budget_approved:
        budget_problems.append("budget is not approved")
    if not plan.budget.within_cap and plan.budget.cap_usd is not None:
        budget_problems.append(
            f"worst-case spend {plan.budget.total_usd:.2f} USD exceeds the cap "
            f"{plan.budget.cap_usd:.2f} USD"
        )
    missing_digests = sorted(
        {cell.task.template.ref for cell in cells if not _is_digest(cell.task.template.digest)}
    )
    missing_probes = sorted(
        {
            cell.model.endpoint_id
            for cell in cells
            if cell.model.endpoint_id not in freeze.probe_report_ids
        }
    )
    unpinned_models = sorted(
        {
            cell.model.slug
            for cell in cells
            if cell.model.slug not in freeze.model_spec_versions
            or (cell.model.self_deployed and freeze.model_revisions.get(cell.model.slug) is None)
        }
    )
    unmapped_templates = sorted(
        {
            cell.task.template.ref
            for cell in cells
            if not freeze.template_digests.get(cell.task.template.ref)
        }
    )
    return FreezeGuards(
        lint_passed=settle("lint_passed", [] if lint_passed else ["task pack lint is not green"]),
        budget_approved=settle("budget_approved", budget_problems),
        image_digests_locked=settle(
            "image_digests_locked", [f"no pinned digest for {ref}" for ref in missing_digests]
        ),
        endpoint_probe_reports_frozen=settle(
            "endpoint_probe_reports_frozen", [f"no probe report for {ep}" for ep in missing_probes]
        ),
        judges_admitted=settle("judges_admitted", judge_problems(judge_facts or {})),
        model_revisions_frozen=settle(
            "model_revisions_frozen", [f"no frozen revision for {slug}" for slug in unpinned_models]
        ),
        template_digest_map_frozen=settle(
            "template_digest_map_frozen",
            [f"no provider digest map for {ref}" for ref in unmapped_templates],
        ),
        gateway_version_frozen=settle(
            "gateway_version_frozen", [] if freeze.gateway_version else ["gateway version is empty"]
        ),
        reasons=reasons,
    )


async def run_guards(
    plan: BatchPlan,
    *,
    endpoint_states: EndpointStates,
    capacity: Callable[[str], InstanceCapacity],
    profiles: Mapping[str, IsolationProfile],
    deadline_s: float | None = None,
    today: date | None = None,
) -> RunGuards:
    """Frozen -> Running: endpoints ``warm``, capacity satisfiable, isolation profiles valid.

    A self-deployed endpoint must be ``WARM`` (probed ``READY`` and warmed to the planned
    replicas); hosted endpoints are not checked here. With ``deadline_s`` the capacity check also
    estimates the serial capacity time each instance needs (trials x lifetime / parallelism).
    """
    cells = plan.runnable_cells
    reasons: dict[str, str] = {}

    not_warm: list[str] = []
    for endpoint_id in sorted({c.model.endpoint_id for c in cells if c.model.self_deployed}):
        state = await endpoint_states.state(endpoint_id)
        if state != EndpointState.WARM:
            not_warm.append(f"{endpoint_id} is {state}, expected warm")
    capacity_problems = _capacity_problems(cells, capacity, deadline_s)
    isolation_problems = _isolation_problems(cells, profiles, today or date.today())
    for name, problems in (
        ("endpoints_ready_warm", not_warm),
        ("carrier_capacity_satisfiable", capacity_problems),
        ("isolation_profiles_valid", isolation_problems),
    ):
        if problems:
            reasons[name] = "; ".join(problems)
    return RunGuards(
        endpoints_ready_warm=not not_warm,
        carrier_capacity_satisfiable=not capacity_problems,
        isolation_profiles_valid=not isolation_problems,
        reasons=reasons,
    )


def _capacity_problems(
    cells: tuple[PlannedCell, ...],
    capacity: Callable[[str], InstanceCapacity],
    deadline_s: float | None,
) -> list[str]:
    problems: list[str] = []
    load_s: dict[str, float] = {}
    for cell in cells:
        requirements = cell.task.requirements
        request = ResourceRequest(requirements.cpus, requirements.memory_mb)
        agent_fit = [i for i in cell.match.agent.candidates if capacity(i).can_ever_fit(request)]
        verifier_fit = [
            i for i in cell.match.verifier.candidates if capacity(i).can_ever_fit(request)
        ]
        if not agent_fit:
            problems.append(f"{cell.cell_id}: no agent instance can host {request}")
            continue
        if not verifier_fit:
            problems.append(f"{cell.cell_id}: no verifier instance can host {request}")
            continue
        parallel = capacity(agent_fit[0]).max_parallel(request)
        if parallel:
            load_s[agent_fit[0]] = (
                load_s.get(agent_fit[0], 0.0)
                + len(cell.trials) * requirements.max_lifetime_s / parallel
            )
    if deadline_s is not None:
        problems.extend(
            f"{instance}: needs {seconds:.0f}s of capacity, deadline is {deadline_s:.0f}s"
            for instance, seconds in sorted(load_s.items())
            if seconds > deadline_s
        )
    return problems


def _isolation_problems(
    cells: tuple[PlannedCell, ...], profiles: Mapping[str, IsolationProfile], today: date
) -> list[str]:
    required: dict[str, int] = {}
    for cell in cells:
        floor = int(cell.task.requirements.min_isolation)
        for instance in (*cell.match.agent.candidates, *cell.match.verifier.candidates):
            required[instance] = max(required.get(instance, 0), floor)
    problems: list[str] = []
    for instance, floor in sorted(required.items()):
        profile = profiles.get(instance)
        if profile is None:
            problems.append(f"{instance}: no isolation profile")
            continue
        missing = [key for key in INVARIANT_IDS if key not in profile.invariants]
        if missing:
            problems.append(f"{instance}: profile lacks {', '.join(missing)}")
        if int(profile.declared_isolation) < floor:
            problems.append(f"{instance}: declared L{int(profile.declared_isolation)} < L{floor}")
        verified = profile.verified_isolation
        if verified is not None and int(verified) < floor:
            problems.append(f"{instance}: verified L{int(verified)} < L{floor}")
        for key, entry in sorted(profile.waivers().items()):
            if not entry.reason:
                problems.append(f"{instance}: waiver {key} has no reason")
            if entry.expires is not None:
                try:
                    expired = date.fromisoformat(entry.expires) < today
                except ValueError:
                    problems.append(f"{instance}: waiver {key} has a bad expiry {entry.expires!r}")
                else:
                    if expired:
                        problems.append(f"{instance}: waiver {key} expired on {entry.expires}")
    return problems
