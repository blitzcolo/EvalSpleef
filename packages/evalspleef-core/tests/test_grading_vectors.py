"""档位判定测试：appendix/grading-vectors.json 的 V01-V12 + 补充边界 + 不变式。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from evalspleef.core.feedback import FeedbackRound
from evalspleef.core.grading import HardGate, compute_grade
from evalspleef.core.models import (
    Budget,
    BudgetUsage,
    Defect,
    DefectStatus,
    FeedbackLevel,
    GradeFlag,
    Severity,
    Tier,
    Validity,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "docs" / "appendix" / "grading-vectors.json"
TRIAL_ID = "web-001.demo-model.claude-code.e1.t01"


def load_vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def build_defects(items: list[dict]) -> list[Defect]:
    defects: list[Defect] = []
    for item in items:
        defects.append(
            Defect(
                defect_id=item["defect_id"],
                rubric_item_id=item["rubric_item_id"],
                severity=Severity(item["severity"]),
                status=DefectStatus(item["status"]),
                first_round=item["first_round"],
                last_round=item.get("last_round", item["first_round"]),
                rounds_failed=tuple(item.get("rounds_failed", (item["first_round"],))),
                fixed_round=item.get("fixed_round"),
                feedback_rounds=item.get("feedback_rounds", 0),
                max_level_during_open=item.get("max_level_during_open"),
            )
        )
    return defects


def build_rounds(items: list[dict]) -> list[FeedbackRound]:
    return [
        FeedbackRound(
            round_no=item["round"],
            level=FeedbackLevel(item["level"]),
            defect_ids=(item["defect_id"],) if item.get("defect_id") else (),
            made_progress=item.get("made_progress"),
        )
        for item in items
    ]


def run_vector(vector: dict, **overrides):
    inputs = vector["inputs"]
    raw_budget = inputs["budget"]
    budget = Budget(
        max_feedback_rounds=int(raw_budget["max_feedback_rounds"]),
        max_nudges=int(raw_budget["max_nudges"]),
    )
    usage = BudgetUsage(
        feedback_rounds=int(raw_budget["feedback_rounds_used"]),
        nudges=int(raw_budget["nudges_used"]),
    )
    if raw_budget.get("exhausted"):
        by = raw_budget.get("exhausted_by") or "rounds"
        if by == "rounds":
            usage.rounds = budget.max_rounds
        elif by == "tokens":
            usage.total_tokens = budget.max_total_tokens
        elif by == "wall_clock":
            usage.wall_clock_s = float(budget.max_wall_clock_s)
        elif by == "cost":
            usage.agent_cost_usd = budget.max_cost_usd
    kwargs = dict(
        trial_id=TRIAL_ID,
        defects=build_defects(inputs.get("defects", [])),
        hard_gates=[HardGate(check_id="HG1", passed=bool(inputs["hard_gates_passed"]))],
        feedback_rounds=build_rounds(inputs.get("feedback_rounds", [])),
        budget=budget,
        usage=usage,
        validity=Validity(inputs["validity"]),
        cheat=bool(inputs.get("cheat_flag", False)),
        manual_reason=inputs.get("manual_reason", ""),
        decided_at="2026-11-06T00:00:00Z",
    )
    kwargs.update(overrides)
    return compute_grade(**kwargs)


class GradingVectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = load_vectors()
        cls.vectors = {item["id"]: item for item in cls.document["vectors"]}
        cls.supplementary = {
            item["id"]: item for item in cls.document.get("supplementary_vectors", [])
        }

    def assert_vector(self, vector: dict) -> None:
        outcome = run_vector(vector)
        expected = vector["expected"]
        self.assertEqual(outcome.rule_id, expected["rule_id"])
        self.assertEqual(None if outcome.tier is None else outcome.tier.value, expected["tier"])
        self.assertEqual(outcome.penalty, expected["penalty"])
        self.assertEqual(sorted(str(flag) for flag in outcome.flags), sorted(expected["flags"]))
        self.assertEqual(outcome.counted_in_leaderboard, expected["counted_in_leaderboard"])
        self.assertEqual(outcome.manual_required, expected["requires_human"])
        if expected.get("no_grade"):
            self.assertIsNone(outcome.grade)
        else:
            self.assertIsNotNone(outcome.grade)
        for key, value in expected["derived"].items():
            self.assertEqual(outcome.derived[key], value, f"{vector['id']} derived.{key}")

    def test_v01_zero_defect_perfect(self):
        self.assert_vector(self.vectors["V01"])

    def test_v02_minor_only_a(self):
        self.assert_vector(self.vectors["V02"])

    def test_v03_nudges_do_not_downgrade(self):
        self.assert_vector(self.vectors["V03"])

    def test_v04_single_round_f1_b(self):
        self.assert_vector(self.vectors["V04"])

    def test_v05_single_round_f1_half_step_bplus(self):
        self.assert_vector(self.vectors["V05"])

    def test_v06_multi_round_f1_c(self):
        self.assert_vector(self.vectors["V06"])

    def test_v07_multi_round_f1_half_step_cplus(self):
        self.assert_vector(self.vectors["V07"])

    def test_v08_f2_d(self):
        self.assert_vector(self.vectors["V08"])

    def test_v09_f3_d(self):
        self.assert_vector(self.vectors["V09"])

    def test_v10_budget_failed(self):
        self.assert_vector(self.vectors["V10"])

    def test_v11_cheat_zeroed(self):
        self.assert_vector(self.vectors["V11"])

    def test_v12_manual_no_grade(self):
        self.assert_vector(self.vectors["V12"])

    def test_v13_format_violation(self):
        vector = self.supplementary["V13"]
        outcome = run_vector(vector, format_violation=True)
        expected = vector["expected"]
        self.assertEqual(outcome.rule_id, expected["rule_id"])
        self.assertEqual(outcome.tier, Tier.F)
        self.assertEqual(outcome.penalty, 0)
        self.assertEqual(sorted(str(flag) for flag in outcome.flags), sorted(expected["flags"]))
        self.assertTrue(outcome.derived["success"])


class SupplementaryBoundaryTest(unittest.TestCase):
    def test_s01_hard_gates_pass_but_blocking_open_budget_exhausted(self):
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=build_defects(
                [
                    {
                        "defect_id": "R1#1",
                        "rubric_item_id": "R1",
                        "severity": "functional",
                        "status": "open",
                        "first_round": 1,
                    }
                ]
            ),
            hard_gates=[HardGate("HG1", True)],
            budget=Budget(max_feedback_rounds=3),
            usage=BudgetUsage(rounds=6, feedback_rounds=3),
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.rule_id, "G-BUDGET-F")
        self.assertEqual(outcome.penalty, 3)
        self.assertEqual(outcome.tier, Tier.F)

    def test_s02_feedback_blocked_abort_rule(self):
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=[],
            hard_gates=[HardGate("HG1", False)],
            feedback_blocked=True,
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.rule_id, "G-ABORT-04")
        self.assertEqual(outcome.tier, Tier.F)
        self.assertIn(GradeFlag.TERMINATED_EARLY, outcome.flags)
        self.assertIn(GradeFlag.FEEDBACK_BLOCKED, outcome.flags)

    def test_s03_anomaly_no_grade(self):
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=[],
            hard_gates=[HardGate("HG1", False)],
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.rule_id, "G-ANOMALY-98")
        self.assertTrue(outcome.manual_required)
        self.assertIsNone(outcome.grade)

    def test_s04_invalid_no_grade(self):
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=[],
            hard_gates=[HardGate("HG1", True)],
            validity=Validity.INVALID,
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.rule_id, "G-INVALID-00")
        self.assertIsNone(outcome.grade)
        self.assertFalse(outcome.manual_required)
        self.assertFalse(outcome.counted_in_leaderboard)

    def test_success_f0_with_blocking_record_is_anomaly(self):
        defect = Defect(
            defect_id="R1#1",
            rubric_item_id="R1",
            severity=Severity.FUNCTIONAL,
            status=DefectStatus.FIXED,
            first_round=1,
            last_round=2,
            fixed_round=2,
            feedback_rounds=0,
        )
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=[defect],
            hard_gates=[HardGate("HG1", True)],
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.rule_id, "G-ANOMALY-98")
        self.assertIsNone(outcome.grade)


class InvariantTest(unittest.TestCase):
    def test_i1_perfect_has_zero_penalty(self):
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=[],
            hard_gates=[HardGate("HG1", True)],
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual((outcome.tier, outcome.penalty), (Tier.P, 0))
        self.assertEqual(outcome.derived["max_level"], "F0")

    def test_i9_penalty_sums_counted_weights(self):
        defects = [
            Defect(
                defect_id="R1#1",
                rubric_item_id="R1",
                severity=Severity.FUNCTIONAL,
                status=DefectStatus.FIXED,
                first_round=1,
                last_round=2,
                fixed_round=2,
                feedback_rounds=1,
                max_level_during_open=FeedbackLevel.F1,
            ),
            Defect(
                defect_id="R1#4",
                rubric_item_id="R1",
                severity=Severity.FUNCTIONAL,
                status=DefectStatus.FIXED,
                first_round=4,
                last_round=5,
                fixed_round=5,
                rounds_failed=(4,),
            ),
            Defect(
                defect_id="R2#1",
                rubric_item_id="R2",
                severity=Severity.MINOR,
                status=DefectStatus.OPEN,
                first_round=1,
                last_round=1,
            ),
        ]
        outcome = compute_grade(
            trial_id=TRIAL_ID,
            defects=defects,
            hard_gates=[HardGate("HG1", True)],
            feedback_rounds=[FeedbackRound(1, FeedbackLevel.F1, ("R1#1",), True)],
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertEqual(outcome.penalty, 3 + 3 + 1)
        self.assertEqual(outcome.grade.defect_summary.total, 3)
        self.assertEqual(outcome.grade.defect_summary.regressed, 1)

    def test_i10_counted_iff_valid(self):
        valid = compute_grade(
            trial_id=TRIAL_ID,
            defects=[],
            hard_gates=[HardGate("HG1", True)],
            decided_at="2026-11-06T00:00:00Z",
        )
        self.assertTrue(valid.counted_in_leaderboard)
        for validity in (Validity.INVALID, Validity.MANUAL):
            outcome = compute_grade(
                trial_id=TRIAL_ID,
                defects=[],
                hard_gates=[HardGate("HG1", True)],
                validity=validity,
                decided_at="2026-11-06T00:00:00Z",
            )
            self.assertFalse(outcome.counted_in_leaderboard)

    def test_i13_f2_f3_implies_d_or_f(self):
        for level in (FeedbackLevel.F2, FeedbackLevel.F3):
            defect = Defect(
                defect_id="R1#1",
                rubric_item_id="R1",
                severity=Severity.FUNCTIONAL,
                status=DefectStatus.FIXED,
                first_round=1,
                last_round=3,
                fixed_round=3,
                feedback_rounds=1,
                max_level_during_open=level,
            )
            outcome = compute_grade(
                trial_id=TRIAL_ID,
                defects=[defect],
                hard_gates=[HardGate("HG1", True)],
                feedback_rounds=[
                    FeedbackRound(1, FeedbackLevel.F1, ("R1#1",), False),
                    FeedbackRound(2, level, ("R1#1",), True),
                ],
                decided_at="2026-11-06T00:00:00Z",
            )
            self.assertIn(outcome.tier, (Tier.D, Tier.F))

    def test_i15_replay_is_deterministic(self):
        document = load_vectors()
        vector = document["vectors"][4]
        results = set()
        for _ in range(10):
            outcome = run_vector(vector)
            results.add(
                (
                    outcome.rule_id,
                    outcome.tier.value if outcome.tier else None,
                    outcome.penalty,
                    tuple(sorted(str(flag) for flag in outcome.flags)),
                )
            )
        self.assertEqual(len(results), 1, results)

    def test_half_step_boundary_integer_comparison(self):
        cases = [
            (1, 3, True),
            (2, 6, True),
            (1, 2, False),
            (1, 4, True),
            (2, 5, False),
        ]
        for need, total, expected in cases:
            defects = []
            for index in range(total):
                is_feedback = index < need
                defects.append(
                    Defect(
                        defect_id=f"R{index + 1}#1",
                        rubric_item_id=f"R{index + 1}",
                        severity=Severity.FUNCTIONAL if is_feedback else Severity.MINOR,
                        status=DefectStatus.FIXED if is_feedback else DefectStatus.OPEN,
                        first_round=1,
                        last_round=2 if is_feedback else 1,
                        fixed_round=2 if is_feedback else None,
                        feedback_rounds=1 if is_feedback else 0,
                        max_level_during_open=FeedbackLevel.F1 if is_feedback else None,
                    )
                )
            rounds = [
                FeedbackRound(index + 1, FeedbackLevel.F1, (f"R{index + 1}#1",), True)
                for index in range(need)
            ]
            outcome = compute_grade(
                trial_id=TRIAL_ID,
                defects=defects,
                hard_gates=[HardGate("HG1", True)],
                feedback_rounds=rounds,
                decided_at="2026-11-06T00:00:00Z",
            )
            self.assertEqual(outcome.derived["half_step"], expected, (need, total))
            self.assertIn(outcome.tier, (Tier.B, Tier.B_PLUS))


if __name__ == "__main__":
    unittest.main()
