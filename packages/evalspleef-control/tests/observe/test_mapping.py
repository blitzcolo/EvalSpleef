"""Check-result to rubric mapping, tested against real pack outputs (fixtures/).

The fixtures are the captured stdout/exit codes of the packs' own hidden tests run
against the reference solutions, the starter (empty answer), wrong answers and
tampered locked files — exactly what the verifier sandbox would capture.
"""

from __future__ import annotations

import json

from evalspleef.control.observe.mapping import (
    check_id_signal,
    load_catalog_detects,
    map_script_items,
)
from evalspleef.control.observe.verifier import GateRun, load_hard_gates
from evalspleef.control.taskpack import TaskPack


def _runs_from_shared_report(pack: TaskPack, fixture: dict[str, object]) -> list[GateRun]:
    """logic/data packs: every gate shares the verify_submission JSON report."""
    report = json.loads(str(fixture["stdout"]))
    runs = []
    for gate in load_hard_gates(pack):
        key = gate.pass_condition.split("==")[0].strip()
        runs.append(
            GateRun(
                gate=gate,
                passed=bool(report.get(key)),
                exit_code=int(fixture["exit_code"]),
                stdout=str(fixture["stdout"]),
                stderr=str(fixture["stderr"]),
                timed_out=False,
            )
        )
    return runs


def _verdicts(pack: TaskPack, runs: list[GateRun]) -> dict[str, str]:
    return {result.item_id: result.verdict for result in map_script_items(pack, runs)}


class TestLogic001:
    def test_reference_passes_script_items(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("logic-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("logic-001.reference"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R1"] == "pass"  # verify_submission.HG2 gate passed
        assert verdicts["R2"] == "pass"  # verify_submission.HG1 gate passed
        assert verdicts["R4"] == "pass"  # verify_submission.HG3 gate passed
        # code-only check ids with no violation in the report carry no signal:
        # undecidable means inconclusive, never a silent pass
        assert verdicts["R3"] == "inconclusive"
        assert verdicts["R5"] == "inconclusive"
        assert verdicts["R11"] == "inconclusive"
        assert verdicts["R12"] == "inconclusive"

    def test_empty_answer_fails_expected_items(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("logic-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("logic-001.empty"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R1"] == "fail"  # HG2_format_ok == false
        assert verdicts["R2"] == "fail"  # HG1_answer_correct == false
        assert verdicts["R4"] == "pass"  # the protected input was not touched
        assert verdicts["R3"] == "inconclusive"
        assert verdicts["R5"] == "inconclusive"

    def test_wrong_answer_fails_via_violation_code(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("logic-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("logic-001.wrong-answer"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R2"] == "fail"  # NEXT_MISMATCH violation code + HG1 failed
        assert verdicts["R1"] == "pass"  # format gate still passed
        assert verdicts["R4"] == "pass"

    def test_tampered_input_fails_the_integrity_item(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("logic-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("logic-001.tampered-input"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R4"] == "fail"  # HG3_input_untampered == false
        assert verdicts["R1"] == "pass"
        assert verdicts["R2"] == "pass"  # the answer itself was correct


class TestData001:
    def test_reference(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("data-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("data-001.reference"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R1"] == "pass"
        assert verdicts["R2"] == "pass"
        assert verdicts["R9"] == "pass"  # HG3 integrity gate
        assert verdicts["R10"] == "pass"  # second HG1-backed item
        assert verdicts["R3"] == "inconclusive"  # format codes absent from a clean run
        assert verdicts["R4"] == "inconclusive"
        assert verdicts["R5"] == "inconclusive"

    def test_empty(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("data-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("data-001.empty"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R1"] == "fail"
        assert verdicts["R2"] == "fail"
        assert verdicts["R9"] == "fail"  # submission missing -> integrity gate fails
        assert verdicts["R10"] == "fail"
        assert verdicts["R3"] == "inconclusive"

    def test_tampered_input(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("data-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("data-001.tampered-input"))
        verdicts = _verdicts(pack, runs)
        assert verdicts["R1"] == "fail"  # sample report no longer matches
        assert verdicts["R2"] == "pass"  # hidden log check still passes
        assert verdicts["R9"] == "fail"  # integrity gate


def _debug_runs(pack: TaskPack, fixture_name: str, hmod) -> list[GateRun]:
    """debug-001: HG1 is run.sh, HG2 check_locks, HG3 the visible-test command."""
    fixture = hmod.load_fixture(fixture_name)
    gates = load_hard_gates(pack)
    assert [g.gate_id for g in gates] == ["HG1", "HG2", "HG3"]
    hg1 = GateRun(
        gates[0],
        passed=int(fixture["exit_code"]) == 0,
        exit_code=int(fixture["exit_code"]),
        stdout=str(fixture["stdout"]),
        stderr=str(fixture["stderr"]),
        timed_out=False,
    )
    lock_fixture = hmod.load_fixture("debug-001.tampered-locks")
    if fixture_name == "debug-001.tampered-test":
        hg2 = GateRun(gates[1], False, 3, "", str(lock_fixture["stderr"]), False)
    else:
        hg2 = GateRun(gates[1], True, 0, "OK: 1 个测试文件哈希未变\n", "", False)
    hg3 = GateRun(gates[2], True, 0, "ok\n", "", False)
    return [hg1, hg2, hg3]


class TestDebug001:
    def test_reference_passes(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("debug-001")
        runs = _debug_runs(pack, "debug-001.reference", hmod)
        verdicts = _verdicts(pack, runs)
        assert all(verdicts[item] == "pass" for item in ("R1", "R2", "R3", "R4", "R5", "R6"))
        assert verdicts["R9"] == "pass"  # check_locks gate passed

    def test_starter_fails_every_regression_item(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("debug-001")
        runs = _debug_runs(pack, "debug-001.starter", hmod)
        verdicts = _verdicts(pack, runs)
        assert all(verdicts[item] == "fail" for item in ("R1", "R2", "R3", "R4", "R5", "R6"))
        assert verdicts["R9"] == "pass"  # locks intact; the failure is real bugs

    def test_tampered_test_fails_and_flags_locks(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("debug-001")
        runs = _debug_runs(pack, "debug-001.tampered-test", hmod)
        verdicts = _verdicts(pack, runs)
        assert all(verdicts[item] == "fail" for item in ("R1", "R2", "R3", "R4", "R5", "R6"))
        assert verdicts["R9"] == "fail"  # HG2 (check_locks) failed


class TestSignalRules:
    """Unit-level coverage of the documented matching rules."""

    def test_failing_test_name_signal(self) -> None:
        lines = [
            "test_x (test_regression.TestMoney) ... FAIL",
            "FAIL: test_money (test_regression.TestMoney)",
            "ok test_fine (test_regression.TestFine)",
        ]
        signal = check_id_signal("TestMoney", gate_passed={}, lines=lines, reports=[])
        assert signal == "fail"
        signal = check_id_signal("TestFine", gate_passed={}, lines=lines, reports=[])
        assert signal == "pass"

    def test_gate_fallback_for_dotted_ids(self) -> None:
        assert (
            check_id_signal(
                "verify_submission.HG2", gate_passed={"HG2": False}, lines=[], reports=[]
            )
            == "fail"
        )
        assert (
            check_id_signal(
                "verify_submission.HG2", gate_passed={"HG2": True}, lines=[], reports=[]
            )
            == "pass"
        )

    def test_false_json_boolean_and_code(self) -> None:
        reports = [
            (
                {"hg2formatok": [False]},
                {"NOT_PURE_JSON"},
            )
        ]
        assert (
            check_id_signal("verify_submission.HG2", gate_passed={}, lines=[], reports=reports)
            == "fail"
        )
        assert (
            check_id_signal("checker.NOT_PURE_JSON", gate_passed={}, lines=[], reports=reports)
            == "fail"
        )
        # a clean report (no codes, all booleans true) gives a pass signal
        clean = [({"hg2formatok": [True], "ruleselfconsistent": [True]}, set())]
        assert (
            check_id_signal("verify_submission.HG2", gate_passed={}, lines=[], reports=clean)
            == "pass"
        )

    def test_conflicting_signals_are_undecidable(self) -> None:
        reports = [({"hg2formatok": [True]}, set())]
        assert (
            check_id_signal("HG2", gate_passed={"HG2": False}, lines=[], reports=reports)
            == "undecidable"
        )

    def test_no_signal_is_undecidable(self) -> None:
        assert (
            check_id_signal("checker.WHATEVER", gate_passed={}, lines=[], reports=[])
            == "undecidable"
        )

    def test_catalog_parsing(self, pack_factory) -> None:
        pack: TaskPack = pack_factory("logic-001")
        detects = load_catalog_detects(pack)
        assert detects["R1"].check_ids == ("verify_submission.HG2", "checker.NOT_PURE_JSON")
        assert detects["R6"].source == "llm_judge"

    def test_llm_judge_items_are_not_script_mapped(self, pack_factory, hmod) -> None:
        pack: TaskPack = pack_factory("logic-001")
        runs = _runs_from_shared_report(pack, hmod.load_fixture("logic-001.reference"))
        results = map_script_items(pack, runs)
        assert all(result.item_id not in ("R6", "R7") for result in results)
