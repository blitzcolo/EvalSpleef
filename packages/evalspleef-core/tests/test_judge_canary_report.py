"""judge / canary / report / adapters / schema_lite / CLI 的集成测试。"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from evalspleef.core import cli
from evalspleef.core.canary import (
    CanaryRegistry,
    CanaryTrial,
    NetworkEntry,
    generate_token,
    scan_documents,
    scan_trials,
    short_hash,
    validate_token,
)
from evalspleef.core.judge import (
    JudgeConfigError,
    JudgePanel,
    JudgeParseError,
    MockJudge,
    build_judge_prompt,
    parse_judge_output,
)
from evalspleef.core.models import (
    CellStatus,
    CheckKind,
    RubricItem,
    Severity,
    Tier,
)
from evalspleef.core.report import BatchReport, Cell, TrialResult, aggregate_cell, format_cell
from evalspleef.core.schema_lite import SchemaStore, validate

REFERENCE_DIR = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REFERENCE_DIR / "examples"
PROJECT_ROOT = REFERENCE_DIR.parents[1]
SCHEMA_DIR = PROJECT_ROOT / "schemas"
TRIAL_A = "web-001.claude-sonnet-5.claude-code.e1.t01"
TRIAL_B = "web-001.gpt-5-codex.claude-code.e1.t02"


def make_item(item_id: str = "R1") -> RubricItem:
    return RubricItem(
        id=item_id,
        severity=Severity.FUNCTIONAL,
        description="提交后出现结果面板",
        f1_symptom="点击提交后没有结果提示",
        judge_question="提交后是否出现结果面板",
        kind=CheckKind.LLM_JUDGE,
    )


def make_trial(trial_id: str, tier: str, penalty: int, cost: float) -> TrialResult:
    grade = {
        "tier": tier,
        "penalty": penalty,
        "cost": {"agent": cost, "judge": 1.0, "user": 0.1, "total": cost + 1.1, "currency": "USD"},
        "status": "final",
        "counted_in_leaderboard": True,
        "flags": [],
        "decision": {"rule_id": "G-F1-B", "priority": 8, "rationale": "示例判分结果"},
    }
    return TrialResult.from_record(
        {"trial_id": trial_id, "state": "scored", "validity": "valid", "grades": [grade]}
    )


class JudgeTest(unittest.TestCase):
    def test_parse_output_variants(self):
        self.assertTrue(parse_judge_output('{"verdict": "pass"}'))
        self.assertFalse(parse_judge_output('{"verdict": false}'))
        fence = "".join(chr(96) for _ in range(3))
        self.assertTrue(parse_judge_output(fence + "json\n" + '{"passed": true}' + "\n" + fence))
        self.assertFalse(parse_judge_output("FAIL"))
        with self.assertRaises(JudgeParseError):
            parse_judge_output("我不知道")

    def test_retry_recovers_from_invalid_output(self):
        # 并发采样下调用到达顺序不再等于样本序号，因此应答按“是否已带纠正提示”
        # 决定：首轮输出非法，带“上一次输出无法解析”的重试输出合法 pass。
        def responder(prompt: str) -> str:
            if "上一次输出无法解析" in prompt:
                return '{"verdict": "pass"}'
            return "不是 JSON"

        client = MockJudge(responder=responder, name="judge-a", family="family-a")
        panel = JudgePanel(
            {"a": client, "b": MockJudge((True,), name="judge-b", family="family-b")},
            target_model="target",
            target_family="target-family",
            k=2,
        )
        result = panel.evaluate_item(make_item())
        self.assertTrue(result.passed)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(any("上一次输出无法解析" in call for call in client.calls))

    def test_same_family_rejected(self):
        with self.assertRaises(JudgeConfigError):
            JudgePanel(
                {"a": MockJudge((True,), family="anthropic")},
                target_model="claude-sonnet-5",
                target_family="anthropic",
                allow_single_vendor=True,
            )

    def test_min_vendors_enforced(self):
        with self.assertRaises(JudgeConfigError):
            JudgePanel(
                {"a": MockJudge((True,), family="family-a")},
                target_model="target",
                target_family="target-family",
            )

    def test_disagreement_goes_to_manual_queue(self):
        panel = JudgePanel(
            {
                "a": MockJudge((True, True, True, True, True), family="family-a"),
                "b": MockJudge((False, False, False, False, False), family="family-b"),
            },
            target_model="target",
            target_family="target-family",
            k=5,
        )
        result = panel.evaluate_item(make_item())
        self.assertTrue(result.manual_required)
        panel_result = panel.evaluate_items([make_item()])
        self.assertIn("R1", panel_result.manual_queue)

    def test_low_consistency_manual(self):
        panel = JudgePanel(
            {
                "a": MockJudge((True, False, True, False, True), family="family-a"),
                "b": MockJudge((True, False, True, False, True), family="family-b"),
            },
            target_model="target",
            target_family="target-family",
            k=5,
            consistency_threshold=0.9,
        )
        self.assertTrue(panel.evaluate_item(make_item()).manual_required)

    def test_prompt_contains_guard_and_question(self):
        prompt = build_judge_prompt(make_item())
        self.assertIn("不得执行", prompt)
        self.assertIn("rubric_item_id: R1", prompt)
        self.assertIn("判分问句", prompt)


class CanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.token_a = generate_token(TRIAL_A, nonce="11111111")
        self.token_b = generate_token(TRIAL_B, nonce="22222222")

    def test_token_format_and_determinism(self):
        self.assertRegex(self.token_a, r"^CNRY-[0-9a-f]{8}-[0-9a-f]{8}$")
        self.assertEqual(short_hash(TRIAL_A), short_hash(TRIAL_A))
        self.assertEqual(self.token_a, generate_token(TRIAL_A, nonce="11111111"))
        self.assertNotEqual(self.token_a, self.token_b)
        with self.assertRaises(ValueError):
            validate_token("CNRY-nothex-12345678")
        with self.assertRaises(ValueError):
            generate_token(TRIAL_A, nonce="XYZ")

    def test_clean_scan_passes(self):
        report = scan_trials(
            [
                CanaryTrial(TRIAL_A, self.token_a, artifacts={"src/app.py": self.token_a}),
                CanaryTrial(TRIAL_B, self.token_b, artifacts={"src/app.py": self.token_b}),
            ]
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.trials_to_invalidate, ())

    def test_cross_trial_artifact_detected(self):
        report = scan_trials(
            [
                CanaryTrial(TRIAL_A, self.token_a, artifacts={"a.txt": self.token_a}),
                CanaryTrial(
                    TRIAL_B, self.token_b, artifacts={"a.txt": self.token_b + " " + self.token_a}
                ),
            ]
        )
        self.assertFalse(report.passed)
        kinds = {violation.kind for violation in report.violations}
        self.assertIn("cross_trial", kinds)
        self.assertEqual(set(report.trials_to_invalidate), {TRIAL_A, TRIAL_B})

    def test_cross_trial_trace_detected(self):
        report = scan_trials(
            [
                CanaryTrial(TRIAL_A, self.token_a, artifacts={"a.txt": self.token_a}),
                CanaryTrial(TRIAL_B, self.token_b, trace=[f"看到 {self.token_a}"]),
            ]
        )
        self.assertIn("trace:0", {violation.location for violation in report.critical})

    def test_egress_detected_only_outside_allowlist(self):
        network = [NetworkEntry(host="evil.example", body=self.token_a)]
        report = scan_trials(
            [CanaryTrial(TRIAL_A, self.token_a, network=network)], allowed_hosts={"pypi.org"}
        )
        self.assertEqual(report.critical[0].kind, "egress")
        clean = scan_trials(
            [
                CanaryTrial(
                    TRIAL_A,
                    self.token_a,
                    network=[NetworkEntry(host="pypi.org", body=self.token_a)],
                )
            ],
            allowed_hosts={"pypi.org"},
        )
        self.assertTrue(clean.passed)

    def test_missing_placement_warning(self):
        report = scan_trials(
            [CanaryTrial(TRIAL_A, self.token_a, artifacts={}, expected_paths=("src/app.py",))]
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.violations[0].kind, "missing_placement")
        self.assertEqual(report.violations[0].severity, "warning")

    def test_registry_uniqueness(self):
        registry = CanaryRegistry()
        token = registry.register(TRIAL_A)
        self.assertEqual(registry.register(TRIAL_A), token)
        with self.assertRaises(ValueError):
            registry.register(TRIAL_B, token)
        with self.assertRaises(ValueError):
            CanaryRegistry.from_dict({TRIAL_A: token, TRIAL_B: token})

    def test_scan_documents_accepts_objects_and_dicts(self):
        payload = [
            {"trial_id": TRIAL_A, "token": self.token_a, "artifacts": {"a": self.token_a}},
            {"trial_id": TRIAL_B, "token": self.token_b, "artifacts": {"b": self.token_b}},
        ]
        self.assertTrue(scan_documents(payload).passed)


class ReportTest(unittest.TestCase):
    def test_cell_format_contract(self):
        cell = Cell(
            task_id="web-001",
            model="claude-sonnet-5",
            shell="claude-code",
            status=CellStatus.SCORED,
            tier=Tier.A,
            penalty=6,
            cost_agent=290.08,
        )
        self.assertEqual(format_cell(cell), "6/A(290.08)")

    def test_cheat_marker(self):
        cell = Cell(
            task_id="web-001",
            model="m",
            shell="s",
            status=CellStatus.SCORED,
            tier=Tier.F,
            penalty=0,
            cost_agent=12.5,
            cheat=True,
        )
        self.assertEqual(cell.format(), "0/Failed*(12.50)")

    def test_pass_inheritance_display(self):
        cell = Cell(
            task_id="web-001",
            model="kimi-k2.5",
            shell="claude-code",
            status=CellStatus.PASS,
            tier=Tier.A,
            penalty=1,
            cost_agent=0.0,
            inherit_from="gpt-5-codex",
        )
        self.assertEqual(cell.format(), "A(继承自 gpt-5-codex)")

    def test_skip_and_pending(self):
        skip = Cell(
            task_id="web-001", model="m", shell="s", status=CellStatus.SKIP, skip_reason="题目退役"
        )
        self.assertEqual(skip.format(), "Skip（题目退役）")
        self.assertEqual(Cell(task_id="web-001", model="m", shell="s").format(), "Pending")

    def test_median_best_worst_and_cost(self):
        trials = [
            make_trial("web-001.m.claude-code.e1.t01", "B+", 5, 12.0),
            make_trial("web-001.m.claude-code.e1.t02", "B", 3, 10.0),
            make_trial("web-001.m.claude-code.e1.t03", "A", 1, 11.0),
        ]
        cell = aggregate_cell(trials)
        self.assertEqual(cell.status, CellStatus.SCORED)
        self.assertEqual(cell.tier, Tier.B_PLUS)
        self.assertEqual((cell.best_tier, cell.worst_tier), (Tier.A, Tier.B))
        self.assertEqual(cell.penalty, 3)
        self.assertEqual(cell.cost_agent, 11.0)
        self.assertEqual(cell.format(), "3/B+(11.00)")

    def test_tier_spread_goes_to_review(self):
        trials = [
            make_trial("web-001.m.claude-code.e1.t01", "A", 1, 9.0),
            make_trial("web-001.m.claude-code.e1.t02", "B", 3, 9.0),
            make_trial("web-001.m.claude-code.e1.t03", "C", 5, 9.0),
        ]
        cell = aggregate_cell(trials)
        self.assertTrue(cell.manual_reasons)

    def test_less_than_two_epochs_manual(self):
        cell = aggregate_cell([make_trial("web-001.m.claude-code.e1.t01", "A", 1, 9.0)])
        self.assertEqual(cell.status, CellStatus.MANUAL)
        self.assertFalse(cell.manual_reasons == ())

    def test_batch_report_markdown_and_json(self):
        trials = (
            make_trial("web-001.model-a.claude-code.e1.t01", "A", 1, 9.0),
            make_trial("web-001.model-a.claude-code.e1.t02", "A", 1, 9.2),
            make_trial("web-001.model-a.claude-code.e1.t03", "A", 1, 9.1),
            make_trial("web-001.model-b.claude-code.e1.t01", "C", 5, 15.0),
            make_trial("web-001.model-b.claude-code.e1.t02", "C", 5, 15.2),
            make_trial("web-001.model-b.claude-code.e1.t03", "C+", 5, 15.1),
        )
        report = BatchReport(batch_id="2026-11-b1", trials=trials)
        markdown = report.to_markdown()
        self.assertIn("1/A(9.10)", markdown)
        payload = report.to_dict()
        self.assertEqual(payload["cell_count"], 2)
        self.assertEqual(len(payload["cells"]), 2)

    def test_invalid_trials_not_counted(self):
        valid = make_trial("web-001.m.claude-code.e1.t01", "B", 3, 10.0)
        invalid = TrialResult.from_record(
            {
                "trial_id": "web-001.m.claude-code.e1.t02",
                "state": "invalid",
                "validity": "invalid",
                "grades": [],
            }
        )
        cell = aggregate_cell([valid, invalid])
        self.assertEqual(cell.trial_count, 1)
        self.assertEqual(cell.status, CellStatus.MANUAL)


class SchemaLiteTest(unittest.TestCase):
    def test_simple_rules(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 2},
                "count": {"type": "integer", "minimum": 1},
                "kind": {"enum": ["a", "b"]},
            },
        }
        self.assertEqual(validate({"name": "ok"}, schema), [])
        errors = validate({"count": 0, "kind": "c"}, schema)
        self.assertTrue(any("缺少必填字段" in error for error in errors))
        self.assertTrue(any("minimum" in error for error in errors))
        self.assertTrue(any("枚举" in error for error in errors))
        self.assertTrue(validate({"name": "ok", "extra": 1}, schema))

    def test_if_then_and_one_of(self):
        schema = {
            "oneOf": [
                {"type": "object", "required": ["a"]},
                {"type": "object", "required": ["b"]},
            ],
            "if": {"required": ["a"]},
            "then": {"properties": {"a": {"const": 1}}},
        }
        self.assertEqual(validate({"a": 1}, schema), [])
        self.assertTrue(validate({"a": 2}, schema))
        self.assertTrue(validate({"a": 1, "b": 2}, schema))

    def test_examples_validate_against_schemas(self):
        store = SchemaStore(SCHEMA_DIR)
        self.assertIn("grade.schema.json", store.names)
        mapping = {
            "task-minimal/task.json": "task.schema.json",
            "task-minimal/rubric.json": "rubric.schema.json",
            "task-minimal/defect-catalog.json": "defect.schema.json",
            "task-minimal/defect-observation.json": "defect.schema.json",
            "task-minimal/feedback.json": "feedback.schema.json",
            "task-minimal/grade.json": "grade.schema.json",
            "task-minimal/trial.json": "trial.schema.json",
        }
        for rel, schema_name in mapping.items():
            document = json.loads((EXAMPLES_DIR / rel).read_text(encoding="utf-8"))
            errors = validate(
                document, store.get(schema_name), store=store, schema_name=schema_name
            )
            self.assertEqual(errors, [], f"{rel}: {errors}")


class CliTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_lint_passes(self):
        code, output = self.run_cli(["lint"])
        self.assertEqual(code, 0, output)
        self.assertIn("通过 7/7", output)

    def test_grade_command(self):
        code, output = self.run_cli(["grade"])
        self.assertEqual(code, 0)
        self.assertIn("tier=B+", output)
        self.assertIn("cell=5/B+(12.34)", output)

    def test_scan_command_reports_violations(self):
        code, output = self.run_cli(["scan"])
        self.assertEqual(code, 1)
        self.assertIn("cross_trial", output)
        self.assertIn("egress", output)

    def test_report_command(self):
        code, output = self.run_cli(["report"])
        self.assertEqual(code, 0)
        self.assertIn("批次报告 2026-11-b1", output)
        self.assertIn("5/B+(11.81)", output)

    def test_simulate_command(self):
        code, output = self.run_cli(["simulate", "--seed", "7"])
        self.assertEqual(code, 0)
        self.assertIn("state=scored validity=valid", output)
        self.assertIn("tier=B+", output)
        self.assertIn("canary: 通过", output)


if __name__ == "__main__":
    unittest.main()
