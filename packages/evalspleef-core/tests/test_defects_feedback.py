"""defects.py 与 feedback.py 测试：缺陷跨轮跟踪、反馈阶梯与泄露检查。"""

from __future__ import annotations

import json
import unittest

from evalspleef.core.defects import DefectCatalog, DefectTracker, UnknownRubricItem
from evalspleef.core.feedback import (
    F0Policy,
    FeedbackDecision,
    FeedbackLadder,
    FeedbackRound,
    LeakCategory,
    RegexLeakChecker,
    allowed_fields,
    build_feedback_record,
    build_leak_check,
    filter_allowed_fields,
    validate_feedback_message,
)
from evalspleef.core.models import (
    Budget,
    BudgetUsage,
    EscalationPolicy,
    FeedbackLevel,
    RubricItem,
    RubricSet,
    Severity,
)

FENCE = "".join(chr(96) for _ in range(3))


def make_catalog() -> DefectCatalog:
    items = [
        RubricItem(
            id="R1",
            severity=Severity.FUNCTIONAL,
            description="提交后出现结果面板",
            f1_symptom="点击提交后页面没有结果提示",
            judge_question="提交后是否出现结果面板",
            f2_evidence="Uncaught TypeError: submitForm is not a function",
            f3_visual="右侧结果区域为空",
        ),
        RubricItem(
            id="R2",
            severity=Severity.FATAL,
            description="刷新后数据仍在",
            f1_symptom="刷新后填写内容消失",
            judge_question="刷新后数据是否仍在",
        ),
        RubricItem(
            id="R3",
            severity=Severity.MINOR,
            description="窄屏按钮不重叠",
            f1_symptom="窄屏下按钮文字重叠",
            judge_question="窄屏下按钮文字是否不重叠",
            hidden=True,
        ),
    ]
    return DefectCatalog.from_rubric_set(RubricSet(task_id="web-001", items=tuple(items)))


def ladder_history(levels, progress, defect_id: str = "R2#1") -> list[FeedbackRound]:
    return [
        FeedbackRound(
            round_no=index + 1, level=level, defect_ids=(defect_id,), made_progress=progress[index]
        )
        for index, level in enumerate(levels)
    ]


class DefectTrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = make_catalog()
        self.tracker = DefectTracker(self.catalog)

    def test_first_round_marks_new_then_open(self):
        report = self.tracker.observe(1, ["R1", "R3"])
        self.assertEqual(report.new, ("R1#1", "R3#1"))
        self.assertEqual(report.open, ("R1#1", "R3#1"))
        self.assertEqual(self.tracker.get("R1#1").status.value, "open")

    def test_second_round_persisting_and_fixed(self):
        self.tracker.observe(1, ["R1", "R3"])
        report = self.tracker.observe(2, ["R3"])
        self.assertEqual(report.persisting, ("R3#1",))
        self.assertEqual(report.fixed, ("R1#1",))
        self.assertEqual(self.tracker.get("R3#1").status.value, "persisting")
        self.assertEqual(self.tracker.get("R1#1").status.value, "fixed")
        self.assertEqual(self.tracker.get("R1#1").rounds_failed, (1,))

    def test_same_round_dedup_and_evidence_merge(self):
        report = self.tracker.observe(1, ["R1", "R1", "R1"])
        self.assertEqual(report.new, ("R1#1",))
        self.tracker.observe(2, ["R1"], evidence={"R1": ["s3://shot-2.png"]})
        self.assertEqual(self.tracker.get("R1#1").rounds_failed, (1, 2))
        self.assertIn("s3://shot-2.png", [item.uri for item in self.tracker.get("R1#1").evidence])

    def test_recurrence_creates_new_defect_and_regression(self):
        self.tracker.observe(1, ["R1"])
        self.tracker.observe(2, [])
        self.assertEqual(self.tracker.get("R1#1").status.value, "fixed")
        report = self.tracker.observe(3, ["R1"])
        self.assertEqual(report.new, ("R1#3",))
        self.assertEqual(len(self.tracker.defects_for_item("R1")), 2)
        self.assertEqual(len(self.tracker.regressed_defects()), 1)

    def test_round_must_increase(self):
        self.tracker.observe(1, ["R1"])
        with self.assertRaises(ValueError):
            self.tracker.observe(1, [])
        with self.assertRaises(ValueError):
            self.tracker.observe(0, [])

    def test_unknown_item_rejected(self):
        with self.assertRaises(UnknownRubricItem):
            self.tracker.observe(1, ["R99"])

    def test_partial_progress(self):
        self.tracker.observe(1, ["R1"])
        report = self.tracker.observe(2, ["R1"], partial_item_ids=["R1"])
        self.assertEqual(self.tracker.get("R1#1").progress.value, "partial")
        self.assertIn("R1#1", report.partial)

    def test_mark_feedback_updates_counts(self):
        self.tracker.observe(1, ["R1"])
        defect = self.tracker.mark_feedback("R1#1", FeedbackLevel.F1)
        self.assertEqual(defect.feedback_rounds, 1)
        defect = self.tracker.mark_feedback("R1#1", FeedbackLevel.F2)
        self.assertEqual(defect.max_level_during_open, FeedbackLevel.F2)
        self.tracker.observe(2, [])
        with self.assertRaises(ValueError):
            self.tracker.mark_feedback("R1#1", FeedbackLevel.F3)

    def test_summary_counts(self):
        self.tracker.observe(1, ["R1", "R3"])
        self.tracker.observe(2, ["R3"])
        summary = self.tracker.summary()
        self.assertEqual(summary["total_discovered"], 2)
        self.assertEqual(summary["blocking_open"], 0)
        self.assertEqual(len(self.tracker.fixed_defects()), 1)


class LadderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = make_catalog()
        self.tracker = DefectTracker(self.catalog)
        self.ladder = FeedbackLadder()
        self.budget = Budget()
        self.usage = BudgetUsage()

    def test_first_decision_is_f1_for_blocking(self):
        self.tracker.observe(1, ["R1"])
        decision = self.ladder.next_decision(self.tracker, [], self.budget, self.usage)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.level, FeedbackLevel.F1)
        self.assertEqual(decision.defect_id, "R1#1")
        self.assertEqual(decision.allowed_fields, frozenset({"f1_symptom"}))

    def test_minor_only_never_gets_feedback(self):
        self.tracker.observe(1, ["R3"])
        self.assertIsNone(self.ladder.next_decision(self.tracker, [], self.budget, self.usage))
        self.assertEqual(
            self.ladder.stop_reason(self.tracker, [], self.budget, self.usage),
            "no_blocking_defects",
        )

    def test_priority_fatal_before_functional(self):
        self.tracker.observe(1, ["R1", "R2"])
        self.assertEqual(self.ladder.select_target(self.tracker).defect_id, "R2#1")

    def test_f1_two_no_progress_then_f2(self):
        self.tracker.observe(1, ["R2"])
        history = ladder_history([FeedbackLevel.F1, FeedbackLevel.F1], [False, False])
        decision = self.ladder.next_decision(self.tracker, history, self.budget, self.usage)
        self.assertEqual(decision.level, FeedbackLevel.F2)
        self.assertIn("F1 连续", decision.reason)

    def test_f2_two_no_progress_then_f3(self):
        self.tracker.observe(1, ["R2"])
        history = ladder_history(
            [FeedbackLevel.F1, FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F2],
            [False, False, False, False],
        )
        decision = self.ladder.next_decision(self.tracker, history, self.budget, self.usage)
        self.assertEqual(decision.level, FeedbackLevel.F3)

    def test_partial_progress_resets_counter(self):
        # docs/03 §7.5：遇到 partial 进展会把“连续无进展”计数清零，不升级。
        self.tracker.observe(1, ["R2"])
        history = [
            FeedbackRound(1, FeedbackLevel.F1, ("R2#1",), made_progress=False),
            FeedbackRound(2, FeedbackLevel.F1, ("R2#1",), made_progress=False, progress="partial"),
        ]
        decision = self.ladder.next_decision(self.tracker, history, self.budget, self.usage)
        self.assertEqual(decision.level, FeedbackLevel.F1)

    def test_f3_stays(self):
        self.tracker.observe(1, ["R2"])
        history = ladder_history(
            [
                FeedbackLevel.F1,
                FeedbackLevel.F1,
                FeedbackLevel.F2,
                FeedbackLevel.F2,
                FeedbackLevel.F3,
            ],
            [False, False, False, False, False],
        )
        decision = self.ladder.next_decision(self.tracker, history, self.budget, self.usage)
        self.assertEqual(decision.level, FeedbackLevel.F3)
        self.assertIn("最高级别", decision.reason)

    def test_budget_exhaustion_stops_feedback(self):
        self.tracker.observe(1, ["R2"])
        usage = BudgetUsage(feedback_rounds=3)
        self.assertIsNone(self.ladder.next_decision(self.tracker, [], self.budget, usage))
        self.assertTrue(
            self.ladder.stop_reason(self.tracker, [], self.budget, usage).startswith(
                "budget_exhausted:"
            )
        )

    def test_allowed_fields_matrix(self):
        self.assertEqual(allowed_fields(FeedbackLevel.F1), frozenset({"f1_symptom"}))
        self.assertEqual(allowed_fields(FeedbackLevel.F2), frozenset({"f1_symptom", "f2_evidence"}))
        self.assertEqual(
            allowed_fields(FeedbackLevel.F3),
            frozenset({"f1_symptom", "f2_evidence", "f3_visual", "f3_assist"}),
        )
        self.assertEqual(allowed_fields(FeedbackLevel.F0), frozenset())
        self.assertEqual(allowed_fields(FeedbackLevel.F2, hidden=True), frozenset({"f1_symptom"}))

    def test_filter_allowed_fields(self):
        kept, dropped = filter_allowed_fields(
            {"f1_symptom": "现象", "f2_evidence": "日志"}, FeedbackLevel.F1
        )
        self.assertEqual(kept, {"f1_symptom": "现象"})
        self.assertEqual(dropped, ("f2_evidence",))
        kept, dropped = filter_allowed_fields(
            {"f1_symptom": "现象", "f2_evidence": "日志"}, FeedbackLevel.F3, hidden=True
        )
        self.assertEqual(kept, {"f1_symptom": "现象"})

    def test_escalation_defaults_from_conventions(self):
        policy = EscalationPolicy()
        self.assertEqual((policy.f1_no_progress_rounds, policy.f2_no_progress_rounds), (2, 2))
        ladder = FeedbackLadder(policy)
        self.assertEqual((ladder.max_f1_no_progress, ladder.max_f2_no_progress), (2, 2))


class LeakCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = RegexLeakChecker(forbidden_terms=("submitForm",))

    def categories(self, text: str, level: FeedbackLevel) -> set[str]:
        return {violation.category.value for violation in self.checker.check(text, level)}

    def test_f1_rejects_file_names_and_fix_suggestions(self):
        categories = self.categories("请把 index.py 里的 render() 改成异步的", FeedbackLevel.F1)
        self.assertIn(LeakCategory.FILE_NAME.value, categories)
        self.assertIn(LeakCategory.FIX_SUGGESTION.value, categories)

    def test_f1_clean_symptom_passes(self):
        self.assertEqual(self.categories("我点提交后页面没有任何结果提示", FeedbackLevel.F1), set())

    def test_f2_allows_log_text(self):
        text = "控制台里有这么一段：Traceback (most recent call last): File app.js line 3"
        self.assertEqual(self.categories(text, FeedbackLevel.F2), set())

    def test_f2_rejects_fix_suggestion(self):
        self.assertIn(
            LeakCategory.FIX_SUGGESTION.value,
            self.categories("你试试把按钮改成禁用的", FeedbackLevel.F2),
        )

    def test_f3_rejects_code(self):
        text = FENCE + "js\nconst a = 1;\n" + FENCE
        self.assertIn(LeakCategory.CODE.value, self.categories(text, FeedbackLevel.F3))

    def test_answer_and_canary_always_blocked(self):
        for level in (FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F3):
            categories = self.categories("正确答案见 CNRY-12345678-abcdef01", level)
            self.assertIn(LeakCategory.ANSWER.value, categories)
            self.assertIn(LeakCategory.CANARY.value, categories)

    def test_forbidden_term(self):
        self.assertIn(
            LeakCategory.ANSWER.value,
            self.categories("submitForm 这个函数没定义", FeedbackLevel.F2),
        )

    def test_multiple_defects_rejected(self):
        result = validate_feedback_message(
            level=FeedbackLevel.F1,
            defect_ids=["R1#1", "R2#1"],
            text="按钮有问题",
            checker=self.checker,
        )
        self.assertFalse(result.ok)
        self.assertIn(
            LeakCategory.MULTIPLE_DEFECTS.value, {item.category.value for item in result.violations}
        )

    def test_nudge_must_not_mention_defect(self):
        result = validate_feedback_message(
            level=FeedbackLevel.F0,
            defect_ids=["R1#1"],
            text="请按你的判断做完。",
            checker=self.checker,
        )
        self.assertFalse(result.ok)

    def test_build_leak_check_and_record(self):
        leak_check = build_leak_check(
            FeedbackLevel.F1,
            "我点提交后没有结果提示",
            self.checker,
            model="guard-small",
            model_verdict="clean",
        )
        self.assertTrue(leak_check["passed"])
        self.assertEqual(leak_check["final_action"], "send")
        blocked = build_leak_check(
            FeedbackLevel.F1, "index.py 里 render() 有问题", self.checker, rewrite_count=3
        )
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["final_action"], "blocked")
        self.assertTrue(blocked["rule_ids_hit"])
        record = build_feedback_record(
            trial_id="web-001.demo-model.claude-code.e1.t01",
            feedback_index=1,
            decision=FeedbackDecision(
                level=FeedbackLevel.F1,
                defect_id="R1#1",
                allowed_fields=frozenset({"f1_symptom"}),
            ),
            text="我点提交后页面没有结果提示",
            template_id="F1-FORM-BLANK",
            decided_at="2026-11-06T00:00:00Z",
            budget=Budget(),
            usage=BudgetUsage(rounds=1),
            checker=self.checker,
            sent_at="2026-11-06T00:00:05Z",
        )
        payload = record.to_dict()
        self.assertEqual(payload["kind"], "feedback")
        self.assertEqual(payload["target_defect_id"], "R1#1")
        self.assertEqual(len(payload["text_hash"]), 64)
        self.assertIn("budget", payload)
        json.dumps(payload)

    def test_nudge_record_rules(self):
        record = build_feedback_record(
            trial_id="web-001.demo-model.claude-code.e1.t01",
            feedback_index=2,
            decision=FeedbackDecision(
                level=FeedbackLevel.F0, defect_id=None, allowed_fields=frozenset()
            ),
            text=F0Policy.GENERIC_CONTINUE,
            template_id="F0-NUDGE-1",
            decided_at="2026-11-06T00:00:00Z",
            budget=Budget(),
            usage=BudgetUsage(rounds=1),
        )
        payload = record.to_dict()
        self.assertEqual(payload["kind"], "nudge")
        self.assertIsNone(payload["target_defect_id"])
        self.assertEqual(payload["allowed_fields"], [])


class F0PolicyTest(unittest.TestCase):
    def test_nudges_capped_by_budget(self):
        policy = F0Policy(budget=Budget(max_nudges=2))
        self.assertEqual(policy.continue_prompt(), F0Policy.GENERIC_CONTINUE)
        self.assertEqual(policy.continue_prompt(), F0Policy.GENERIC_CONTINUE)
        self.assertIsNone(policy.continue_prompt())
        self.assertEqual(policy.remaining_nudges, 0)

    def test_faq_and_generic_answer(self):
        policy = F0Policy(faq={"支持哪些浏览器": "请以题目要求为目标浏览器。"})
        self.assertEqual(
            policy.respond_to_question("请问支持哪些浏览器？"), "请以题目要求为目标浏览器。"
        )
        self.assertEqual(policy.respond_to_question("用哪个库？"), F0Policy.GENERIC_ANSWER)


if __name__ == "__main__":
    unittest.main()
