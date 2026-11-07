"""生成 reference/examples 下的样例文档，并用 schema_lite 校验（开发辅助脚本）。"""

from __future__ import annotations

import json
from pathlib import Path

from evalspleef.core.canary import generate_token
from evalspleef.core.feedback import (
    FeedbackRound,
    RegexLeakChecker,
    build_feedback_record,
)
from evalspleef.core.grading import HardGate, compute_grade
from evalspleef.core.models import (
    Budget,
    BudgetUsage,
    CheckKind,
    Defect,
    EscalationPolicy,
    Evidence,
    EvidenceKind,
    FeedbackLevel,
    RubricItem,
    RubricSet,
    Severity,
    Validity,
)
from evalspleef.core.schema_lite import SchemaStore, validate
from evalspleef.core.state import TrialMachine, record_grade

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "reference" / "examples"
SCHEMA_DIR = ROOT / "schemas"
TRIAL_ID = "web-001.claude-sonnet-5.claude-code.e1.t01"
TASK_ID = "web-001"
BATCH_ID = "2026-11-b1"
CELL_ID = "web-001.claude-sonnet-5.claude-code"
TOKEN = generate_token(TRIAL_ID, nonce="9f3a1c02")

ITEMS = (
    RubricItem(
        id="R1",
        severity=Severity.FUNCTIONAL,
        description="提交表单后出现成功结果面板",
        f1_symptom="填写表单点击提交后，页面没有任何结果提示",
        judge_question="提交表单后是否出现成功结果面板",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="提交后 3 秒内出现带成功文案的结果面板",
        applies_to=("screenshot",),
        f2_evidence="Uncaught TypeError: submitForm is not a function",
        f3_visual="截图右侧结果区域为空，按钮保持禁用状态",
    ),
    RubricItem(
        id="R2",
        severity=Severity.FUNCTIONAL,
        description="刷新页面后已提交的数据仍在",
        f1_symptom="刷新页面后刚才填写的内容全部消失",
        judge_question="刷新页面后已提交的数据是否仍然存在",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="刷新后列表仍显示刚提交的记录",
        applies_to=("screenshot",),
    ),
    RubricItem(
        id="R3",
        severity=Severity.MINOR,
        description="按钮在窄屏下不换行",
        f1_symptom="窗口变窄时按钮文字挤在一起",
        judge_question="窄屏下按钮文字是否不重叠",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="360px 宽时按钮文字不重叠",
        applies_to=("screenshot",),
        hidden=True,
    ),
    RubricItem(
        id="R4",
        severity=Severity.MINOR,
        description="错误提示文案语气一致",
        f1_symptom="错误提示有的很正式有的很随意",
        judge_question="错误提示文案语气是否一致",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="全部错误提示使用同一语气模板",
        applies_to=("screenshot",),
    ),
    RubricItem(
        id="R5",
        severity=Severity.MINOR,
        description="深色模式下对比度达标",
        f1_symptom="深色模式里部分文字看不清",
        judge_question="深色模式下文字与背景对比度是否达标",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="深色模式文字对比度 >= 4.5:1",
        applies_to=("screenshot",),
    ),
    RubricItem(
        id="R6",
        severity=Severity.MINOR,
        description="提交按钮有加载状态",
        f1_symptom="点击提交后按钮没有反应提示",
        judge_question="提交过程中按钮是否显示加载状态",
        kind=CheckKind.LLM_JUDGE,
        pass_criteria="提交后 300ms 内按钮出现 loading 状态",
        applies_to=("screenshot",),
        hidden=True,
    ),
)
RUBRIC = RubricSet(task_id=TASK_ID, items=ITEMS, rubric_version="1.3.0", title="订单表单页")

DEFECTS = (
    Defect(
        defect_id="R1#1",
        rubric_item_id="R1",
        severity=Severity.FUNCTIONAL,
        status="fixed",
        first_round=1,
        last_round=2,
        rounds_failed=(1,),
        fixed_round=2,
        feedback_rounds=1,
        max_level_during_open=FeedbackLevel.F1,
        progress="fixed",
        evidence=(
            Evidence(kind=EvidenceKind.SCREENSHOT, uri="s3://llm-eval/web-001/r1-round1.png"),
        ),
    ),
    Defect(
        defect_id="R3#1",
        rubric_item_id="R3",
        severity=Severity.MINOR,
        status="open",
        first_round=1,
        last_round=2,
        rounds_failed=(1, 2),
        fixed_round=None,
        feedback_rounds=0,
        max_level_during_open=None,
        progress="none",
        evidence=(
            Evidence(kind=EvidenceKind.SCREENSHOT, uri="s3://llm-eval/web-001/r3-round2.png"),
        ),
    ),
    Defect(
        defect_id="R4#1",
        rubric_item_id="R4",
        severity=Severity.MINOR,
        status="open",
        first_round=1,
        last_round=2,
        rounds_failed=(1, 2),
        fixed_round=None,
        feedback_rounds=0,
        max_level_during_open=None,
        progress="none",
        evidence=(),
    ),
)
ROUNDS = (
    FeedbackRound(round_no=1, level=FeedbackLevel.F1, defect_ids=("R1#1",), made_progress=True),
)
USAGE = BudgetUsage(
    rounds=2,
    feedback_rounds=1,
    nudges=0,
    total_tokens=180_000,
    wall_clock_s=420.0,
    agent_cost_usd=12.34,
    judge_cost_usd=1.2,
    user_cost_usd=0.3,
)
BUDGET = Budget()
GATES = (
    HardGate(check_id="HG1", passed=True, evidence_uri="s3://llm-eval/web-001/hidden-tests.log"),
    HardGate(check_id="HG2", passed=True),
)
OUTCOME = compute_grade(
    trial_id=TRIAL_ID,
    defects=DEFECTS,
    hard_gates=GATES,
    feedback_rounds=ROUNDS,
    budget=BUDGET,
    usage=USAGE,
    validity=Validity.VALID,
    judge_consistency_value=0.9,
    judge_samples=10,
    decided_at="2026-11-06T08:10:00Z",
    rubric_version="1.3.0",
    task_version="1.2.0",
)
GRADE = OUTCOME.grade
assert GRADE is not None

CHECKER = RegexLeakChecker(forbidden_terms=("submitForm", "is not a function"))

# 构造反馈文档（直接使用控制器决策类型的轻量实例，避免依赖状态机）。
from evalspleef.core.feedback import FeedbackDecision

LADDER_DECISION = FeedbackDecision(
    level=FeedbackLevel.F1,
    defect_id="R1#1",
    allowed_fields=frozenset({"f1_symptom"}),
    reason="缺陷 R1#1 首次反馈，从最低级别 F1 开始",
)
FEEDBACK_TEXT = "我填好表单点提交以后，页面没有任何成功提示，也没有报错；我期望看到结果面板。"
FEEDBACK = build_feedback_record(
    trial_id=TRIAL_ID,
    feedback_index=1,
    decision=LADDER_DECISION,
    text=FEEDBACK_TEXT,
    template_id="F1-FORM-BLANK",
    decided_at="2026-11-06T07:52:00Z",
    budget=BUDGET,
    usage=BudgetUsage(rounds=1, total_tokens=90_000, wall_clock_s=210.0, agent_cost_usd=6.1),
    checker=CHECKER,
    sent_at="2026-11-06T07:52:05Z",
)
assert FEEDBACK.leak_check["passed"], FEEDBACK.to_dict()


def defect_summary_item(defect: Defect, *, status: str) -> dict:
    payload = defect.to_summary_item()
    payload["status"] = status
    return payload


def build_trial() -> dict:
    machine = TrialMachine(TRIAL_ID)
    for event in ("lease", "start", "stop", "needs_feedback", "deliver", "stop", "finish", "score"):
        machine.apply(event)
    record_grade(machine.record, GRADE)
    return {
        "schema_version": "1.0",
        "trial_id": TRIAL_ID,
        "batch_id": BATCH_ID,
        "cell_id": CELL_ID,
        "task": {"id": TASK_ID, "version": "1.2.0", "track": "web", "platform": "linux-container"},
        "model": {"slug": "claude-sonnet-5", "provider": "anthropic", "version": "2026-10-01"},
        "shell": {"slug": "claude-code", "version": "2.0.14", "mode": "native"},
        "epoch": 1,
        "index": 1,
        "state": str(machine.state),
        "validity": str(machine.validity),
        "terminal_condition": "P2",
        "budget": {
            **BUDGET.to_dict(),
            "escalation": EscalationPolicy().to_dict(),
        },
        "rounds": [
            {
                "round_no": 1,
                "started_at": "2026-11-06T07:40:00Z",
                "stop_reason": "completed",
                "usage": {"input_tokens": 60_000, "output_tokens": 30_000, "cost_usd": 6.1},
                "hard_gates": [
                    {"check_id": "HG1", "result": "fail"},
                    {"check_id": "HG2", "result": "fail"},
                ],
                "defects": [
                    defect_summary_item(DEFECTS[0], status="open"),
                    defect_summary_item(DEFECTS[1], status="new"),
                    defect_summary_item(DEFECTS[2], status="new"),
                ],
                "feedback": FEEDBACK.to_dict(),
            },
            {
                "round_no": 2,
                "started_at": "2026-11-06T07:58:00Z",
                "stop_reason": "completed",
                "usage": {"input_tokens": 60_000, "output_tokens": 30_000, "cost_usd": 6.24},
                "hard_gates": [
                    {"check_id": "HG1", "result": "pass"},
                    {"check_id": "HG2", "result": "pass"},
                ],
                "defects": [
                    defect_summary_item(DEFECTS[0], status="fixed"),
                    defect_summary_item(DEFECTS[1], status="persisting"),
                    defect_summary_item(DEFECTS[2], status="persisting"),
                ],
                "feedback": None,
            },
        ],
        "isolation": {
            "sandbox": {
                "provider": "docker",
                "host": "host-07",
                "image_digest": "sha256:" + "a" * 64,
                "isolation_mode": "container",
                "network_mode": "off",
                "root_readonly": True,
                "non_root_user": "agent",
                "quota": {
                    "cpu": 2,
                    "memory_mb": 4096,
                    "disk_mb": 10240,
                    "pids": 512,
                    "wall_clock_s": 3600,
                },
                "lease_id": "lease-web001-e1-t01",
            },
            "canary": {
                "token": TOKEN,
                "placements": [
                    {"path": "src/app.js", "kind": "comment"},
                    {"path": "data/sample.json", "kind": "sample_data"},
                ],
                "hits": [],
                "scan_status": "clean",
            },
            "checks": [{"id": "I1", "result": "pass"}],
            "leaks": [],
            "network": {
                "mode": "off",
                "allowlist": [],
                "blocked_requests": 0,
                "bypass_attempts": 0,
            },
            "secrets": {
                "api_key_in_sandbox": False,
                "ssh_control_plane_only": True,
                "per_trial_key": True,
                "log_redaction": True,
            },
        },
        "result": {
            "hard_gates": [
                {"check_id": "HG1", "result": "pass"},
                {"check_id": "HG2", "result": "pass"},
            ],
            "defects": [
                defect_summary_item(DEFECTS[0], status="fixed"),
                defect_summary_item(DEFECTS[1], status="persisting"),
                defect_summary_item(DEFECTS[2], status="persisting"),
            ],
            "grades": [GRADE.to_dict()],
            "cost": GRADE.cost.to_dict(),
            "versions": {
                "task_version": "1.2.0",
                "model_version": "2026-10-01",
                "shell_version": "2.0.14",
                "image_digest": "sha256:" + "a" * 64,
                "rubric_version": "1.3.0",
                "grader_version": "evalspleef-core-0.1.0",
                "freeze_hash": "b" * 64,
            },
        },
        "timestamps": {"created_at": "2026-11-06T07:30:00Z"},
        "audit": {"events_uri": "s3://llm-eval/audit/web-001.e1.t01.jsonl"},
    }


def build_task() -> dict:
    return {
        "id": TASK_ID,
        "version": "1.2.0",
        "track": "web",
        "title": "订单表单页（最小示例）",
        "platform": "linux-container",
        "difficulty": 3,
        "instruction": "instruction.md",
        "persona": "persona.md",
        "rubric_ref": "rubric.yaml",
        "defects_ref": "defects.yaml",
        "isolation": {
            "agent_user": "agent",
            "environment_mode": "separate",
            "network_mode": "no-network",
        },
        "reference_solutions": [
            {
                "id": "a",
                "path": "answers/reference-a.html",
                "approach": "原生 JS + DOM API",
                "runnable": False,
                "notes": "Node 本地起静态服务器验证。",
            },
            {
                "id": "b",
                "path": "answers/reference-b.tsx",
                "approach": "React 受控组件",
                "runnable": False,
                "notes": "Vite 构建后静态验证。",
            },
        ],
        "resources": {
            "image": "ghcr.io/llm-eval/web-001",
            "image_digest": "sha256:" + "a" * 64,
            "cpu": 2,
            "memory_mb": 4096,
            "disk_mb": 10240,
            "network": False,
            "tools": ["screenshot", "browser"],
        },
        "budget": {**BUDGET.to_dict(), "escalation": EscalationPolicy().to_dict()},
        "hard_gates": [
            {
                "id": "HG1",
                "title": "可见交互测试全过",
                "kind": "script",
                "script": "bash hidden-tests/run.sh",
                "required": True,
                "pass_condition": "退出码 0 且 JSON 结果 passed=true",
                "description": "Playwright 驱动表单提交与刷新断言。",
            },
            {
                "id": "HG2",
                "title": "产物文件存在",
                "kind": "script",
                "script": "python3 hidden-tests/check_artifacts.py",
                "required": True,
                "pass_condition": "退出码 0",
                "description": "检查构建产物与 source map。",
            },
        ],
        "canary": {
            "manifest": "canary.md",
            "locations": ["src/app.js#L1 注释", "data/sample.json 的 note 字段"],
            "expected_trace": ["第 1 次 read src/app.js"],
        },
        "status": "active",
    }


def build_observation() -> dict:
    return {
        "document_type": "observation",
        "schema_version": "1.0",
        "trial_id": TRIAL_ID,
        "round": 2,
        "observed_at": "2026-11-06T08:05:00Z",
        "source": "mixed",
        "items": [defect.to_dict() for defect in DEFECTS],
        "summary": {
            "new": 0,
            "persisting": 2,
            "fixed": 1,
            "open": 2,
            "blocking_open": 0,
            "total_discovered": 3,
            "defects_requiring_feedback": 1,
            "by_severity": {"minor": 2, "functional": 1, "fatal": 0},
        },
    }


def build_catalog() -> dict:
    return {
        "document_type": "catalog",
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "catalog_version": "1.3.0",
        "items": [
            {
                "rubric_item_id": "R1",
                "title": "提交后无结果面板",
                "severity": "functional",
                "f1_symptom": "填写表单点击提交后，页面没有任何结果提示",
                "f2_evidence": {
                    "kind": "runtime_trace",
                    "text": "Uncaught TypeError: submitForm is not a function",
                    "source_command": "npm run build && node server.js",
                    "redactions": ["<PATH>"],
                },
                "f3_visual": {
                    "description": "右侧结果区域为空，提交按钮保持禁用",
                    "regions": ["右栏 x≈900,y≈300"],
                },
                "f3_assist": {
                    "action": "click",
                    "instruction": "我来帮你点一下提交按钮",
                    "reason": "按钮被浮层遮挡",
                },
                "detect": {"source": "script", "check_ids": ["HG1"]},
                "evidence_required": ["screenshot"],
            }
        ],
    }


def build_internal_examples() -> dict[str, dict]:
    """evalspleef.core CLI 的内部输入样例（无对应 JSON Schema，格式由 CLI 定义）。"""
    grading_case = {
        "trial_id": "web-001.demo-model.claude-code.e1.t01",
        "validity": "valid",
        "hard_gates": [
            {"check_id": "HG1", "passed": True},
            {"check_id": "HG2", "passed": True},
        ],
        "defects": [
            {
                "defect_id": "R1#1",
                "rubric_item_id": "R1",
                "severity": "functional",
                "status": "fixed",
                "first_round": 1,
                "last_round": 2,
                "rounds_failed": [1],
                "fixed_round": 2,
                "feedback_rounds": 1,
                "max_level_during_open": "F1",
                "progress": "fixed",
                "evidence": [],
            },
            {
                "defect_id": "R3#1",
                "rubric_item_id": "R3",
                "severity": "minor",
                "status": "open",
                "first_round": 1,
                "last_round": 2,
                "rounds_failed": [1, 2],
                "fixed_round": None,
                "feedback_rounds": 0,
                "max_level_during_open": None,
                "progress": "none",
                "evidence": [],
            },
            {
                "defect_id": "R4#1",
                "rubric_item_id": "R4",
                "severity": "minor",
                "status": "open",
                "first_round": 1,
                "last_round": 2,
                "rounds_failed": [1, 2],
                "fixed_round": None,
                "feedback_rounds": 0,
                "max_level_during_open": None,
                "progress": "none",
                "evidence": [],
            },
        ],
        "feedback_rounds": [
            {"round": 1, "level": "F1", "defect_ids": ["R1#1"], "made_progress": True}
        ],
        "budget": {**BUDGET.to_dict(), "escalation": EscalationPolicy().to_dict()},
        "usage": USAGE.to_dict(),
        "judge_consistency": 0.9,
        "judge_samples": 10,
        "decided_at": "2026-11-06T08:10:00Z",
        "rubric_version": "1.3.0",
        "task_version": "1.2.0",
        "boundary_review": False,
    }
    token_a = generate_token("web-001.claude-sonnet-5.claude-code.e1.t01", nonce="11111111")
    token_b = generate_token("web-001.gpt-5-codex.claude-code.e1.t01", nonce="22222222")
    token_c = generate_token("web-001.gemini-3-pro.claude-code.e1.t01", nonce="33333333")
    canary_trials = {
        "allowed_hosts": ["pypi.org"],
        "trials": [
            {
                "trial_id": "web-001.claude-sonnet-5.claude-code.e1.t01",
                "token": token_a,
                "artifacts": {"src/app.js": f"// canary: {token_a}\nexport const app = 1;\n"},
                "trace": [f"read src/app.js（{token_a}）"],
                "network": [],
                "expected_paths": ["src/app.js"],
            },
            {
                "trial_id": "web-001.gpt-5-codex.claude-code.e1.t01",
                "token": token_b,
                "artifacts": {
                    "src/app.js": f"// canary: {token_b}\n# 上一轮残留: {token_a}\n",
                },
                "trace": [],
                "network": [],
                "expected_paths": [],
            },
            {
                "trial_id": "web-001.gemini-3-pro.claude-code.e1.t01",
                "token": token_c,
                "artifacts": {"src/app.js": f"// canary: {token_c}\n"},
                "trace": [],
                "network": [
                    {
                        "host": "evil.example",
                        "url": "https://evil.example/upload",
                        "body": f"answer={token_c}",
                    }
                ],
                "expected_paths": ["src/app.js"],
            },
        ],
    }

    def trial_summary(model: str, index: int, tier: str, penalty: int, cost: float) -> dict:
        tier_numeric = {"P": 7, "A": 6, "B+": 5, "B": 4, "C+": 3, "C": 2, "D": 1, "F": 0}[tier]
        return {
            "trial_id": f"web-001.{model}.claude-code.e1.t{index:02d}",
            "state": "scored",
            "validity": "valid",
            "grades": [
                {
                    "grade_version": 1,
                    "supersedes": None,
                    "tier": tier,
                    "tier_numeric": tier_numeric,
                    "status": "final",
                    "validity": "valid",
                    "counted_in_leaderboard": True,
                    "flags": [],
                    "decided_by": "auto",
                    "decided_at": "2026-11-06T09:00:00Z",
                    "hard_gate_summary": {"passed": True, "total": 2, "failed": []},
                    "defect_summary": {
                        "total": penalty,
                        "by_severity": {"minor": penalty, "functional": 0, "fatal": 0},
                        "open_blocking": 0,
                        "fixed": 0,
                        "requiring_feedback": 0,
                        "multi_round_f1": 0,
                        "regressed": 0,
                    },
                    "feedback_summary": {
                        "max_level": "F1",
                        "feedback_rounds": 1,
                        "level_counts": {"F0": 0, "F1": 1, "F2": 0, "F3": 0},
                        "nudges_used": 0,
                        "total_defects": penalty,
                        "need_feedback": 0,
                        "half_step_applied": False,
                        "multi_round_f1": 0,
                    },
                    "penalty": penalty,
                    "penalty_breakdown": [],
                    "cost": {
                        "agent": cost,
                        "judge": 1.2,
                        "user": 0.3,
                        "total": round(cost + 1.5, 6),
                        "currency": "USD",
                    },
                    "consistency": {"metric": "percent_agreement", "value": 0.9, "samples": 10},
                    "decision": {
                        "rule_id": "G-F1-B",
                        "priority": 8,
                        "rationale": "示例批次记录（README 演示用，非真实评测结果）",
                    },
                    "rubric_version": "1.3.0",
                    "task_version": "1.2.0",
                    "judge_panel": [],
                }
            ],
            "errors": [],
            "extensions": {},
        }

    trials = [
        trial_summary("claude-sonnet-5", 1, "B+", 5, 12.34),
        trial_summary("claude-sonnet-5", 2, "B+", 5, 11.81),
        trial_summary("claude-sonnet-5", 3, "B", 3, 10.95),
        trial_summary("gpt-5-codex", 1, "A", 1, 9.10),
        trial_summary("gpt-5-codex", 2, "A", 1, 8.77),
        trial_summary("gpt-5-codex", 3, "A", 2, 9.02),
        trial_summary("gemini-3-pro", 1, "C", 4, 15.20),
        trial_summary("gemini-3-pro", 2, "C+", 4, 14.88),
        trial_summary("gemini-3-pro", 3, "C", 5, 16.01),
        {
            "trial_id": "web-001.kimi-k2.5.claude-code.e1.t01",
            "state": "pending",
            "validity": "pending",
            "grades": [],
            "errors": [],
            "extensions": {},
        },
        {
            "trial_id": "web-001.deepseek-v4.claude-code.e1.t01",
            "state": "pending",
            "validity": "pending",
            "grades": [],
            "errors": [],
            "extensions": {},
        },
    ]
    batch_input = {
        "batch_id": BATCH_ID,
        "generated_at": "2026-11-06T10:00:00Z",
        "trials": trials,
        "inherit": {
            "web-001.kimi-k2.5.claude-code": {
                "task_id": "web-001",
                "model": "gpt-5-codex",
                "shell": "claude-code",
                "tier": "A",
                "penalty": 1,
                "cost_agent": 9.10,
            }
        },
        "skips": {"web-001.deepseek-v4.claude-code": "题目退役（v2 计划重出）"},
    }
    return {
        "grading-case.json": grading_case,
        "canary-trials.json": canary_trials,
        "batch-2026-11-b1/batch-input.json": batch_input,
    }


def dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    store = SchemaStore(SCHEMA_DIR)
    documents = {
        "task-minimal/task.json": ("task.schema.json", build_task()),
        "task-minimal/rubric.json": ("rubric.schema.json", RUBRIC.to_document()),
        "task-minimal/defect-catalog.json": ("defect.schema.json", build_catalog()),
        "task-minimal/defect-observation.json": ("defect.schema.json", build_observation()),
        "task-minimal/feedback.json": ("feedback.schema.json", FEEDBACK.to_dict()),
        "task-minimal/grade.json": ("grade.schema.json", GRADE.to_dict()),
        "task-minimal/trial.json": ("trial.schema.json", build_trial()),
    }
    failures = 0
    for rel, (schema_name, document) in documents.items():
        errors = validate(document, store.get(schema_name), store=store, schema_name=schema_name)
        if errors:
            failures += 1
            print(f"[FAIL] {rel}")
            for error in errors:
                print("   ", error)
        else:
            print(f"[ok] {rel}")
    if failures:
        return 1
    for rel, (_schema_name, document) in documents.items():
        dump(EXAMPLES / rel, document)
    for rel, payload in build_internal_examples().items():
        dump(EXAMPLES / rel, payload)
    print("[ok] 内部 CLI 输入样例已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
