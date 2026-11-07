"""The sandbox basic case library against the builtin mock sandbox target."""

from __future__ import annotations

import pytest

from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.case import Case
from evalspleef.conformance.cases_sandbox_basic import CASES, LARGE_FILE_BYTES_ENV
from evalspleef.conformance.target import ConformanceTarget

EXPECTED_IDS = (
    "C-LIFE-01",
    "C-LIFE-02",
    "C-LIFE-03",
    "C-LIFE-04",
    "C-EXEC-01",
    "C-EXEC-02",
    "C-EXEC-03",
    "C-EXEC-04",
    "C-EXEC-05",
    "C-EXEC-06",
    "C-EXEC-07",
    "C-FILE-01",
    "C-FILE-02",
    "C-FILE-03",
    "C-FILE-04",
)


@pytest.fixture()
def sandbox_target() -> ConformanceTarget:
    (target,) = [target for target in build_targets() if target.name == "mock-sandbox"]
    return target


def test_case_ids_are_exact_and_unique() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(EXPECTED_IDS)
    assert len(set(ids)) == len(ids), "a case id appears more than once"
    assert set(ids) == set(EXPECTED_IDS), f"unexpected case ids: {sorted(set(ids))}"
    assert all(ids.count(case_id) == 1 for case_id in EXPECTED_IDS)


def test_all_cases_target_the_sandbox_spi() -> None:
    assert all(case.kind == "sandbox" for case in CASES)
    assert all(case.title.strip() for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_case_passes_or_is_not_applicable(
    case: Case,
    sandbox_target: ConformanceTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LARGE_FILE_BYTES_ENV, str(1 << 20))
    reason = case.applies(sandbox_target)
    if reason is not None:
        assert isinstance(reason, str) and reason.strip(), "not-applicable reason is empty"
        return
    # A failing case raises AssertionError, which fails this test with its message.
    await case.run(sandbox_target)
