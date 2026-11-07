"""models.py 的契约测试：枚举拼写、档位映射、预算、ID 与序列化。"""

from __future__ import annotations

import json
import unittest

from evalspleef.core.grading import HardGate, compute_grade
from evalspleef.core.models import (
    BOUNDARY_TIERS,
    CELL_FORMAT,
    Budget,
    BudgetUsage,
    CheckKind,
    Cost,
    DecidedBy,
    DefectStatus,
    Disposition,
    ErrorClass,
    FeedbackLevel,
    Grade,
    GradeFlag,
    GradeStatus,
    RubricItem,
    RubricSet,
    RuleId,
    Severity,
    Tier,
    TrialError,
    TrialId,
    TrialRecord,
    TrialState,
    Validity,
    batch_id_is_valid,
    defect_id_for,
    display_tier,
    format_trial_id,
    max_feedback_level,
    median_tier,
    parse_batch_id,
    parse_defect_id,
    parse_trial_id,
    tier_by_value,
    tier_value,
    to_plain,
)
from evalspleef.core.models import (
    Disposition as _Disposition,
)


class EnumContractTest(unittest.TestCase):
    def test_trial_state_spelling(self):
        self.assertEqual(
            {state.value for state in TrialState},
            {
                "pending",
                "leased",
                "running",
                "observing",
                "awaiting_feedback",
                "verifying",
                "scored",
                "invalid",
                "paused_manual",
                "failed_infra",
            },
        )

    def test_feedback_level_spelling(self):
        self.assertEqual([level.value for level in FeedbackLevel], ["F0", "F1", "F2", "F3"])

    def test_tier_order_and_numeric_mapping(self):
        expected = ["P", "A", "B+", "B", "C+", "C", "D", "F"]
        self.assertEqual([tier.value for tier in Tier], expected)
        self.assertEqual([tier_value(tier) for tier in Tier], [7, 6, 5, 4, 3, 2, 1, 0])
        self.assertEqual(tier_by_value(5), Tier.B_PLUS)
        self.assertEqual(display_tier(Tier.P), "Perfect")
        self.assertEqual(display_tier(Tier.F), "Failed")
        self.assertEqual(BOUNDARY_TIERS, frozenset({Tier.P, Tier.F}))

    def test_shared_enums(self):
        self.assertEqual({item.value for item in Severity}, {"minor", "functional", "fatal"})
        self.assertEqual(
            {item.value for item in CheckKind},
            {"script", "llm_judge", "agent_judge", "human"},
        )
        self.assertEqual(
            {item.value for item in ErrorClass},
            {
                "provider",
                "model",
                "cheat",
                "isolation",
                "environment",
                "verification",
                "judge",
                "version",
            },
        )
        self.assertEqual(
            {item.value for item in Disposition},
            {"retry", "score", "zero", "manual", "discard"},
        )
        self.assertEqual(
            {item.value for item in Validity}, {"valid", "invalid", "pending", "manual"}
        )
        self.assertEqual(
            {item.value for item in DefectStatus}, {"new", "open", "persisting", "fixed"}
        )
        self.assertEqual(
            {item.value for item in GradeStatus}, {"final", "provisional", "superseded"}
        )
        self.assertEqual({item.value for item in DecidedBy}, {"auto", "human", "arbiter"})
        self.assertIn("cheat", {flag.value for flag in GradeFlag})
        self.assertIn("G-ABORT-04", {rule.value for rule in RuleId})

    def test_cell_format_constant(self):
        self.assertEqual(CELL_FORMAT.format(penalty=6, tier="A", cost=290.08), "6/A(290.08)")
        self.assertEqual(CELL_FORMAT.format(penalty=0, tier="Failed", cost=1.5), "0/Failed(1.50)")


class IdentifierTest(unittest.TestCase):
    def test_trial_id_roundtrip(self):
        raw = "web-001.claude-sonnet-5.claude-code.e1.t02"
        parts = parse_trial_id(raw)
        self.assertIsInstance(parts, TrialId)
        self.assertEqual(parts.task_id, "web-001")
        self.assertEqual(parts.model, "claude-sonnet-5")
        self.assertEqual(parts.shell, "claude-code")
        self.assertEqual((parts.epoch, parts.index), (1, 2))
        self.assertEqual(format_trial_id(parts), raw)

    def test_trial_id_invalid(self):
        for raw in (
            "web-1.demo.shell.e1.t01",
            "web-001.demo.shell.1.t01",
            "web-001.demo.shell.e1.t1",
        ):
            with self.assertRaises(ValueError):
                parse_trial_id(raw)

    def test_batch_id(self):
        self.assertEqual(parse_batch_id("2026-11-b1").seq, 1)
        self.assertTrue(batch_id_is_valid("2026-01-b12"))
        self.assertFalse(batch_id_is_valid("2026-13-b1"))

    def test_defect_id(self):
        self.assertEqual(defect_id_for("R3", 2), "R3#2")
        self.assertEqual(parse_defect_id("R3#2"), ("R3", 2))
        with self.assertRaises(ValueError):
            defect_id_for("X3", 1)
        with self.assertRaises(ValueError):
            parse_defect_id("R3-2")

    def test_median_tier(self):
        self.assertEqual(median_tier([Tier.B, Tier.A, Tier.C]), Tier.B)
        self.assertEqual(median_tier([Tier.A, Tier.C]), Tier.C)
        with self.assertRaises(ValueError):
            median_tier([])

    def test_max_feedback_level(self):
        self.assertIsNone(max_feedback_level([]))
        self.assertEqual(
            max_feedback_level([FeedbackLevel.F1, FeedbackLevel.F3, FeedbackLevel.F2]),
            FeedbackLevel.F3,
        )


class BudgetContractTest(unittest.TestCase):
    def test_frozen_field_names_and_defaults(self):
        budget = Budget()
        self.assertEqual(
            set(budget.to_dict()),
            {
                "max_rounds",
                "max_feedback_rounds",
                "max_nudges",
                "max_total_tokens",
                "max_wall_clock_s",
                "max_cost_usd",
                "max_observe_s",
            },
        )
        self.assertEqual(
            (budget.max_rounds, budget.max_feedback_rounds, budget.max_nudges), (6, 3, 2)
        )
        heavy = Budget.heavy()
        self.assertEqual((heavy.max_rounds, heavy.max_feedback_rounds), (10, 5))

    def test_budget_usage_exhaustion(self):
        budget = Budget()
        usage = BudgetUsage(rounds=6)
        self.assertIn("rounds=6/6", usage.exhausted_reasons(budget))
        self.assertEqual(usage.exhausted_by(budget), "rounds")
        self.assertFalse(BudgetUsage().can_continue(Budget(max_rounds=0)))
        usage = BudgetUsage(feedback_rounds=3)
        self.assertTrue(
            any("feedback_rounds" in reason for reason in usage.exhausted_reasons(budget))
        )
        usage = BudgetUsage(total_tokens=2_000_000)
        self.assertEqual(usage.exhausted_by(budget), "tokens")
        usage = BudgetUsage(agent_cost_usd=15.0)
        self.assertEqual(usage.exhausted_by(budget), "cost")
        usage = BudgetUsage(wall_clock_s=3600.0)
        self.assertEqual(usage.exhausted_by(budget), "wall_clock")

    def test_budget_usage_round_accounting(self):
        usage = BudgetUsage()
        usage.add_round(tokens=100, agent_cost_usd=0.5, wall_clock_s=10.0)
        usage.add_feedback_round(user_cost_usd=0.1)
        usage.add_nudge(user_cost_usd=0.05)
        self.assertEqual((usage.rounds, usage.feedback_rounds, usage.nudges), (1, 1, 1))
        self.assertAlmostEqual(usage.cost.total, 0.65)


class PayloadTest(unittest.TestCase):
    def test_rubric_item_derived_weight_and_document(self):
        item = RubricItem(
            id="R1",
            severity=Severity.FUNCTIONAL,
            description="提交后出现结果面板",
            f1_symptom="点击提交后页面没有结果提示",
            judge_question="提交后是否出现结果面板",
        )
        self.assertEqual(item.weight, 3)
        payload = item.to_dict()
        self.assertEqual(payload["id"], "R1")
        self.assertIn("kind", payload)
        self.assertIn("f1_symptom", payload)
        self.assertNotIn("weight", payload)  # weight 由 severity 派生，不在 schema 字段里
        rubric_doc = RubricSet(task_id="web-001", items=(item,)).to_document()
        self.assertEqual(rubric_doc["schema_version"], "1.0")
        self.assertEqual(rubric_doc["task_id"], "web-001")

    def test_grade_derived_fields(self):
        grade = Grade(
            trial_id="web-001.demo-model.claude-code.e1.t01",
            tier=Tier.B_PLUS,
            decision=__import__("evalspleef.core.models", fromlist=["Decision"]).Decision(
                rule_id=RuleId.F1_BPLUS, priority=6, rationale="单轮 F1 修复并应用半档规则"
            ),
            decided_at="2026-11-06T00:00:00Z",
            penalty=5,
        )
        self.assertEqual(grade.tier_numeric, 5)
        self.assertTrue(grade.counted_in_leaderboard)
        payload = grade.to_dict()
        self.assertEqual(payload["tier_numeric"], 5)
        self.assertTrue(payload["counted_in_leaderboard"])
        self.assertEqual(payload["cost"]["currency"], "USD")
        grade.validity = Validity.MANUAL
        self.assertFalse(grade.counted_in_leaderboard)

    def test_trial_error_disposition(self):
        error = TrialError(error_class=ErrorClass.PROVIDER, cause="ApiRateLimitError")
        self.assertIs(error.disposition.value, "retry")
        self.assertEqual(error.validity, Validity.INVALID)
        self.assertTrue(error.retryable)
        with self.assertRaises(ValueError):
            TrialError(error_class=ErrorClass.PROVIDER, disposition=_Disposition.SCORE)

    def test_trial_record_grade_versions(self):
        record = TrialRecord(trial_id="web-001.demo-model.claude-code.e1.t01")
        self.assertEqual(record.task_id, "web-001")
        self.assertEqual(record.model, "demo-model")
        self.assertIsNone(record.current_grade())

    def test_to_plain_json_serializable(self):
        payload = to_plain(
            {"state": TrialState.SCORED, "tier": Tier.A, "flags": (GradeFlag.CHEAT,)}
        )
        self.assertEqual(payload, {"state": "scored", "tier": "A", "flags": ["cheat"]})
        json.dumps(payload)


class GradeValidationTest(unittest.TestCase):
    def test_cheat_grade_schema_constraints(self):
        outcome = compute_grade(
            trial_id="web-001.demo-model.claude-code.e1.t01",
            defects=[],
            hard_gates=[HardGate("HG1", True)],
            cheat=True,
            cheat_evidence="测试文件哈希变化",
            decided_at="2026-11-06T00:00:00Z",
        )
        grade = outcome.grade
        self.assertIsNotNone(grade)
        assert grade is not None
        self.assertEqual(grade.tier, Tier.F)
        self.assertEqual(grade.penalty, 0)
        self.assertIn(GradeFlag.CHEAT, grade.flags)
        self.assertTrue(grade.counted_in_leaderboard)

    def test_human_grade_requires_notes(self):
        with self.assertRaises(ValueError):
            Grade(
                trial_id="web-001.demo-model.claude-code.e1.t01",
                tier=Tier.B,
                decision=__import__("evalspleef.core.models", fromlist=["Decision"]).Decision(
                    rule_id=RuleId.F1_B, priority=8, rationale="人工覆盖机器结论示例"
                ),
                decided_at="2026-11-06T00:00:00Z",
                decided_by=DecidedBy.HUMAN,
            )

    def test_cost_rejects_negative(self):
        with self.assertRaises(ValueError):
            Cost(agent=-1.0)


if __name__ == "__main__":
    unittest.main()
