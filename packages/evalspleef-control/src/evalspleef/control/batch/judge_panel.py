"""Judge panel admission facts for the freeze guard (PRD appendix C.5, TECH-DESIGN 6.4).

A judge route is admitted only when the endpoint's required judge probes (P-01, P-02,
P-07, P-14) pass and the endpoint state serves traffic, so a frozen batch never starts
trials whose judge calls would fail on a withdrawn gateway route.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from evalspleef.spi.model import EndpointState

__all__ = ["SERVING_STATES", "JudgeGuardFacts", "judge_problems"]

#: Endpoint states whose gateway route serves traffic (mirrors ``EndpointManager``).
SERVING_STATES: frozenset[EndpointState] = frozenset({EndpointState.READY, EndpointState.WARM})


@dataclass(frozen=True)
class JudgeGuardFacts:
    """Admission facts of one judge endpoint, gathered before the freeze guards run.

    Judges are resolved from the batch panel (``judges`` plus ``agent_judge`` judges).
    A judge is admitted only when its endpoint is registered, sits in a state that
    serves traffic (:data:`SERVING_STATES`) and its latest stored probe report passes
    the judge-required probes P-01, P-02, P-07 and P-14 (TECH-DESIGN 6.4, PRD C.5).
    ``endpoint_id``/``state`` are ``None`` when the judge model or its endpoint row
    does not exist; ``report_id`` is ``None`` when no probe report is stored.
    """

    slug: str
    endpoint_id: str | None
    state: EndpointState | None
    report_id: str | None
    failed_probes: tuple[str, ...]


def judge_problems(facts: Mapping[str, JudgeGuardFacts]) -> list[str]:
    """One reason per judge that is not admitted; empty when the whole panel serves."""
    problems: list[str] = []
    for slug in sorted(facts):
        fact = facts[slug]
        name = f"judge {slug}" if fact.endpoint_id is None else f"judge {slug} ({fact.endpoint_id})"
        if fact.endpoint_id is None or fact.state is None:
            problems.append(f"{name} is not registered")
            continue
        parts: list[str] = []
        if fact.state not in SERVING_STATES:
            parts.append(f"is {fact.state.value}, expected ready or warm")
        if fact.report_id is None:
            parts.append("has no probe report")
        elif fact.failed_probes:
            parts.append(f"failed required probes {', '.join(fact.failed_probes)}")
        if parts:
            problems.append(f"{name} " + " and ".join(parts))
    return problems
