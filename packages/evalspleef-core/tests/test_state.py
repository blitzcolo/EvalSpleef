"""state.py 测试：docs/02 §4.2 合法转移、P0-P5 终态、重判与人工覆盖。"""

from __future__ import annotations

import unittest

from evalspleef.core.models import (
    DecidedBy,
    Decision,
    Grade,
    GradeStatus,
    RuleId,
    Tier,
    TrialState,
    Validity,
)
from evalspleef.core.state import (
    EVENT_DESCRIPTIONS,
    MAX_INFRA_RETRIES,
    TERMINAL_STATES,
    IllegalTransition,
    RegradeNotAllowed,
    TrialMachine,
    apply_manual_override,
    apply_terminal,
    describe_transitions,
    evaluate_terminal,
    record_grade,
    regrade,
)

TRIAL_ID = "web-001.demo-model.claude-code.e1.t01"


def make_grade(tier_value: str = "B") -> Grade:
    return Grade(
        trial_id=TRIAL_ID,
        tier=Tier(tier_value),
        decision=Decision(rule_id=RuleId.F1_B, priority=8, rationale="测试用判分结果示例"),
        decided_at="2026-11-06T00:00:00Z",
        penalty=3,
    )


def drive_to_scored(machine: TrialMachine) -> None:
    for event in ("lease", "start", "stop", "needs_feedback", "deliver", "stop", "finish", "score"):
        machine.apply(event)


class TransitionTableTest(unittest.TestCase):
    def test_happy_path_six_states(self):
        machine = TrialMachine(TRIAL_ID)
        expected = [
            (TrialState.PENDING, "lease", TrialState.LEASED),
            (TrialState.LEASED, "start", TrialState.RUNNING),
            (TrialState.RUNNING, "stop", TrialState.OBSERVING),
            (TrialState.OBSERVING, "needs_feedback", TrialState.AWAITING_FEEDBACK),
            (TrialState.AWAITING_FEEDBACK, "deliver", TrialState.RUNNING),
            (TrialState.RUNNING, "stop", TrialState.OBSERVING),
            (TrialState.OBSERVING, "finish", TrialState.VERIFYING),
            (TrialState.VERIFYING, "score", TrialState.SCORED),
        ]
        for from_state, event, to_state in expected:
            self.assertEqual(machine.state, from_state)
            machine.apply(event)
            self.assertEqual(machine.state, to_state)
        self.assertEqual(machine.validity, Validity.VALID)
        self.assertEqual(len(machine.history), len(expected))

    def test_illegal_transition_message(self):
        machine = TrialMachine(TRIAL_ID)
        with self.assertRaises(IllegalTransition) as caught:
            machine.apply("score")
        message = str(caught.exception)
        self.assertIn("pending", message)
        self.assertIn("lease", message)
        self.assertEqual(caught.exception.allowed, machine.allowed_events())

    def test_all_events_have_descriptions(self):
        for state, events in describe_transitions().items():
            for event in events:
                self.assertIn(event, EVENT_DESCRIPTIONS, f"{state} 的 {event} 缺说明")

    def test_awaiting_feedback_three_verifying_triggers(self):
        for trigger in ("feedback_blocked", "budget_exhausted", "finish"):
            machine = TrialMachine(TRIAL_ID)
            for event in ("lease", "start", "stop", "needs_feedback"):
                machine.apply(event)
            machine.apply(trigger)
            self.assertEqual(machine.state, TrialState.VERIFYING, trigger)


class ExceptionPathsTest(unittest.TestCase):
    def test_failed_infra_requeue_then_retry_budget(self):
        machine = TrialMachine(TRIAL_ID)
        machine.apply("lease")
        machine.apply("fail_infra")
        self.assertEqual(machine.state, TrialState.FAILED_INFRA)
        self.assertEqual(machine.validity, Validity.INVALID)
        for _ in range(MAX_INFRA_RETRIES):
            machine.apply("requeue")
            self.assertEqual(machine.state, TrialState.PENDING)
            self.assertEqual(machine.validity, Validity.PENDING)
            machine.apply("lease")
            machine.apply("fail_infra")
        with self.assertRaises(IllegalTransition):
            machine.apply("requeue")
        machine.apply("invalidate")
        self.assertEqual(machine.state, TrialState.INVALID)

    def test_scored_can_be_invalidated_post_hoc(self):
        machine = TrialMachine(TRIAL_ID)
        drive_to_scored(machine)
        self.assertIn(machine.state, TERMINAL_STATES)
        machine.apply("invalidate_post_hoc", note="金丝雀跨 trial")
        self.assertEqual(machine.state, TrialState.INVALID)
        self.assertEqual(machine.validity, Validity.INVALID)

    def test_paused_manual_resume_paths(self):
        for resume_event, expected in (
            ("resume_running", TrialState.RUNNING),
            ("resume_observing", TrialState.OBSERVING),
            ("resume_verifying", TrialState.VERIFYING),
        ):
            machine = TrialMachine(TRIAL_ID, state=TrialState.PAUSED_MANUAL)
            machine.apply(resume_event)
            self.assertEqual(machine.state, expected)
        machine = TrialMachine(TRIAL_ID, state=TrialState.PAUSED_MANUAL)
        machine.apply("invalidate")
        self.assertEqual(machine.state, TrialState.INVALID)

    def test_pause_from_running_and_generic_resume(self):
        machine = TrialMachine(TRIAL_ID)
        machine.apply("lease")
        machine.apply("start")
        machine.apply("pause")
        self.assertEqual(machine.state, TrialState.PAUSED_MANUAL)
        self.assertEqual(machine.validity, Validity.MANUAL)
        machine.apply("resume")
        self.assertEqual(machine.state, TrialState.RUNNING)
        self.assertEqual(machine.validity, Validity.PENDING)

    def test_pause_not_allowed_from_pending(self):
        machine = TrialMachine(TRIAL_ID)
        with self.assertRaises(IllegalTransition):
            machine.apply("pause")


class TerminalPriorityTest(unittest.TestCase):
    def test_priority_short_circuit(self):
        p0 = evaluate_terminal(isolation_failed=True, cheat_detected=True)
        self.assertEqual(p0.code, "P0")
        self.assertEqual(p0.terminal_state, TrialState.INVALID)
        p1 = evaluate_terminal(cheat_detected=True, hard_gates_passed=True)
        self.assertEqual(p1.code, "P1")
        self.assertTrue(p1.scoring)
        p2 = evaluate_terminal(hard_gates_passed=True)
        self.assertEqual(p2.code, "P2")
        p3 = evaluate_terminal(hard_gates_passed=False, budget_exhausted=True)
        self.assertEqual((p3.code, p3.validity), ("P3", Validity.VALID))
        p4 = evaluate_terminal(cannot_continue=True)
        self.assertEqual((p4.code, p4.terminal_state), ("P4", TrialState.VERIFYING))
        p5 = evaluate_terminal(manual_interrupt_expired=True)
        self.assertEqual((p5.code, p5.validity), ("P5", Validity.INVALID))
        self.assertIsNone(evaluate_terminal())

    def test_blocking_defect_prevents_p2(self):
        decision = evaluate_terminal(
            hard_gates_passed=True,
            has_unresolved_blocking_defect=True,
            budget_exhausted=True,
        )
        self.assertEqual(decision.code, "P3")

    def test_apply_terminal_invalid_paths(self):
        machine = TrialMachine(TRIAL_ID)
        decision = evaluate_terminal(isolation_failed=True)
        apply_terminal(machine, decision)
        self.assertEqual(machine.state, TrialState.INVALID)
        machine = TrialMachine(TRIAL_ID)
        drive_to_scored(machine)
        apply_terminal(machine, decision)
        self.assertEqual(machine.state, TrialState.INVALID)

    def test_apply_terminal_verifying_path(self):
        machine = TrialMachine(TRIAL_ID)
        for event in ("lease", "start", "stop"):
            machine.apply(event)
        decision = evaluate_terminal(hard_gates_passed=True)
        apply_terminal(machine, decision)
        self.assertEqual(machine.state, TrialState.VERIFYING)
        machine.apply("score")
        self.assertEqual(machine.state, TrialState.SCORED)


class GradeVersionTest(unittest.TestCase):
    def test_record_grade_then_regrade(self):
        machine = TrialMachine(TRIAL_ID)
        with self.assertRaises(RegradeNotAllowed):
            record_grade(machine.record, make_grade())
        drive_to_scored(machine)
        first = record_grade(machine.record, make_grade("B"))
        self.assertEqual((first.grade_version, first.supersedes), (1, None))
        second = regrade(machine.record, make_grade("B+"), reason="裁判重校准")
        self.assertEqual((second.grade_version, second.supersedes), (2, 1))
        self.assertEqual(machine.record.grades[0].status, GradeStatus.SUPERSEDED)
        self.assertEqual(machine.state, TrialState.SCORED)

    def test_regrade_requires_existing_grade(self):
        machine = TrialMachine(TRIAL_ID)
        drive_to_scored(machine)
        with self.assertRaises(RegradeNotAllowed):
            regrade(machine.record, make_grade())

    def test_manual_override_creates_human_version(self):
        machine = TrialMachine(TRIAL_ID)
        drive_to_scored(machine)
        record_grade(machine.record, make_grade("B"))
        override = make_grade("A")
        applied = apply_manual_override(
            machine.record,
            override,
            reviewer="reviewer-01",
            reason="人工核对截图后确认应升 A 档",
            at="2026-11-06T12:00:00Z",
        )
        self.assertEqual(applied.decided_by, DecidedBy.HUMAN)
        self.assertEqual(applied.human_override.previous_tier.value, "B")
        self.assertEqual(applied.grade_version, 2)
        self.assertEqual(applied.supersedes, 1)
        self.assertTrue(applied.manual_notes)

    def test_grade_requires_scored_state(self):
        machine = TrialMachine(TRIAL_ID)
        with self.assertRaises(RegradeNotAllowed):
            record_grade(machine.record, make_grade())

    def test_manual_override_rejects_short_reason(self):
        machine = TrialMachine(TRIAL_ID)
        drive_to_scored(machine)
        record_grade(machine.record, make_grade())
        with self.assertRaises(ValueError):
            apply_manual_override(
                machine.record,
                make_grade("A"),
                reviewer="r",
                reason="短",
                at="2026-11-06T12:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
