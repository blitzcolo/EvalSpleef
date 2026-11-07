"""Tests for the sandbox environment conformance cases.

Every case runs against the builtin ``mock-sandbox`` target: it must either pass or
report a non-empty not-applicable reason. A case that answers not-applicable is
never executed, so nothing passes silently.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.case import Case
from evalspleef.conformance.cases_sandbox_env import CASES
from evalspleef.conformance.target import ConformanceTarget

EXPECTED_IDS = {
    "C-NET-01",
    "C-NET-02",
    "C-NET-03",
    "C-NET-04",
    "C-NET-05",
    "C-ISO-01",
    "C-ISO-02",
    "C-ISO-03",
    "C-ISO-04",
    "C-ISO-05",
    "C-ISO-06",
    "C-CONC-01",
    "C-FAIL-01",
    "C-FAIL-02",
    "C-SNAP-01",
    "C-SNAP-02",
    "C-GUI-01",
    "C-PORT-01",
}


def _mock_sandbox() -> ConformanceTarget:
    return next(target for target in build_targets() if target.name == "mock-sandbox")


def _case(case_id: str) -> Case:
    return next(case for case in CASES if case.id == case_id)


@dataclass(frozen=True)
class _ObservedTarget(ConformanceTarget):
    """A target that also tells the case where its sandbox sees this process."""

    reachable_from_sandbox: str | None = None


def _observed_mock(host: str) -> ConformanceTarget:
    base = _mock_sandbox()
    return _ObservedTarget(
        name=base.name,
        kind=base.kind,
        provider=base.provider,
        fake=base.fake,
        spec_factory=base.spec_factory,
        injectors=base.injectors,
        notes=base.notes,
        reachable_from_sandbox=host,
    )


def test_case_ids_are_exactly_the_spec_set() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_IDS


def test_case_contract_fields() -> None:
    for case in CASES:
        assert case.kind == "sandbox", case.id
        assert case.title.strip(), case.id
        assert callable(case.applies), case.id
        assert callable(case.run), case.id


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_every_case_passes_or_is_not_applicable(case: Case) -> None:
    target = _mock_sandbox()
    reason = case.applies(target)
    if reason is None:
        await case.run(target)
        return
    assert isinstance(reason, str), case.id
    assert reason.strip(), f"{case.id} reported not-applicable without a reason"


async def test_the_library_actually_runs_cases_against_the_double() -> None:
    target = _mock_sandbox()
    applicable = [case.id for case in CASES if case.applies(target) is None]
    not_applicable = [case.id for case in CASES if case.applies(target) is not None]
    # Both lists are expected to be non-trivial: an all-not-applicable library would
    # prove nothing about the target, and an all-applicable one would mean some case
    # ignores a capability the double does not have.
    assert len(applicable) >= 5, applicable
    assert len(not_applicable) >= 5, not_applicable
    assert sorted(applicable + not_applicable) == sorted(EXPECTED_IDS)


async def test_network_case_detects_a_reachable_canary_under_off() -> None:
    """Negative control: the double does not enforce 'off', so C-NET-01 must fail.

    This proves the case probes a real canary and does not pass by construction.
    """
    target = _observed_mock("127.0.0.1")
    case = _case("C-NET-01")
    assert case.applies(target) is None
    with pytest.raises(AssertionError, match="must not be"):
        await case.run(target)


def test_observer_cases_are_not_applicable_without_an_observer() -> None:
    target = _mock_sandbox()
    for case_id in ("C-NET-01", "C-NET-02"):
        reason = _case(case_id).applies(target)
        assert reason is not None, case_id
        assert "reachable_from_sandbox" in reason, case_id
    # C-NET-05 stops at the missing dynamic-policy capability before it needs an observer.
    reason = _case("C-NET-05").applies(target)
    assert reason is not None
    assert "dynamic_network_policy" in reason
