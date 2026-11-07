"""evalspleef.core.cli — 参考实现命令行入口。

用法：
    python3 -m evalspleef.core lint   [path ...]     # 用 schemas/*.schema.json 校验 examples
    python3 -m evalspleef.core grade  [input.json]   # 跑确定性档位聚合
    python3 -m evalspleef.core scan   [input.json]   # 金丝雀跨 trial 扫描
    python3 -m evalspleef.core report [input.json]   # 生成批次榜单报告（markdown/JSON）
    python3 -m evalspleef.core simulate [--seed N]   # 端到端模拟一个 trial（状态机+阶梯+判分）

退出码：0=成功/通过；1=校验失败或发现隔离违规；2=参数/文件错误。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .canary import CanaryRegistry, CanaryTrial, scan_documents
from .defects import DefectCatalog, DefectTracker
from .feedback import (
    FeedbackLadder,
    FeedbackRound,
)
from .grading import HardGate, compute_grade
from .judge import JudgePanel, MockJudge
from .models import (
    Budget,
    BudgetUsage,
    CellStatus,
    CheckKind,
    Defect,
    Evidence,
    FeedbackLevel,
    RubricItem,
    RubricSet,
    Severity,
    Tier,
    TrialState,
    Validity,
)
from .report import BatchReport, Cell, TrialResult
from .schema_lite import SchemaStore, load_json
from .state import (
    TrialMachine,
    evaluate_terminal,
    record_grade,
)

#: Repository root; this file lives at packages/evalspleef-core/src/evalspleef/core/cli.py.
PROJECT_ROOT = Path(__file__).resolve().parents[5]
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"
DEFAULT_SCHEMA_DIR = PROJECT_ROOT / "schemas"

#: 文件名/内容 -> schema 文件的推断规则。
_SCHEMA_BY_FILENAME: dict[str, str] = {
    "rubric": "rubric.schema.json",
    "defect-observation": "defect.schema.json",
    "defect-catalog": "defect.schema.json",
    "feedback": "feedback.schema.json",
    "grade": "grade.schema.json",
    "trial": "trial.schema.json",
    "task": "task.schema.json",
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    """以 UTF-8 友好格式打印 JSON（不转义中文）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: str | Path) -> Any:
    """读取 JSON 文件；失败抛 SystemExit(2)。"""
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[error] 读取 {path} 失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _infer_schema_name(document: Any, filename: str) -> str | None:
    """根据文档内容/文件名推断应使用的 schema；无法判断返回 None。"""
    if not isinstance(document, dict):
        return None
    if document.get("document_type") == "observation":
        return "defect.schema.json"
    if document.get("document_type") == "catalog":
        return "defect.schema.json"
    if "feedback_index" in document and "leak_check" in document:
        return "feedback.schema.json"
    if "grade_version" in document and "decision" in document:
        return "grade.schema.json"
    if "rubric_version" in document and "items" in document and "task_id" in document:
        return "rubric.schema.json"
    if "trial_id" in document and "state" in document and "grades" in document:
        return "trial.schema.json"
    if "trials" in document or "feedback_rounds" in document:
        return None  # evalspleef.core CLI 内部输入（无 JSON Schema）
    if (
        "trial_id" in document
        and "budget" in document
        and "hard_gates" in document
        and "platform" in document
    ):
        return "task.schema.json"
    stem = Path(filename).stem
    for prefix, schema_name in _SCHEMA_BY_FILENAME.items():
        if stem == prefix or stem.startswith(prefix):
            return schema_name
    return None


def _budget_usage_from_dict(data: Mapping[str, Any]) -> BudgetUsage:
    """从 JSON 构造 BudgetUsage（兼容 cost 拆分）。"""
    return BudgetUsage(
        rounds=int(data.get("rounds", 0)),
        feedback_rounds=int(data.get("feedback_rounds", 0)),
        nudges=int(data.get("nudges", 0)),
        total_tokens=int(data.get("total_tokens", 0)),
        wall_clock_s=float(data.get("wall_clock_s", 0.0)),
        agent_cost_usd=float(data.get("agent_cost_usd", data.get("agent", 0.0))),
        judge_cost_usd=float(data.get("judge_cost_usd", data.get("judge", 0.0))),
        user_cost_usd=float(data.get("user_cost_usd", data.get("user", 0.0))),
    )


def _defect_from_dict(data: Mapping[str, Any]) -> Defect:
    """从 JSON 构造 Defect（progress/evidence 兼容字符串）。"""
    rounds = tuple(data.get("rounds_failed", (data.get("first_round", 1),)))
    return Defect(
        defect_id=str(data["defect_id"]),
        rubric_item_id=str(data["rubric_item_id"]),
        severity=Severity(str(data["severity"])),
        status=str(data["status"]),
        first_round=int(data["first_round"]),
        last_round=int(data.get("last_round", data["first_round"])),
        rounds_failed=rounds,
        fixed_round=data.get("fixed_round"),
        feedback_rounds=int(data.get("feedback_rounds", 0)),
        max_level_during_open=(
            None
            if data.get("max_level_during_open") is None
            else FeedbackLevel(str(data["max_level_during_open"]))
        ),
        progress=str(data.get("progress", "none")),
        evidence=tuple(Evidence.from_any(item) for item in data.get("evidence", ())),
    )


def _feedback_round_from_dict(data: Mapping[str, Any]) -> FeedbackRound:
    """从 JSON 构造 FeedbackRound。"""
    return FeedbackRound(
        round_no=int(data["round"]),
        level=FeedbackLevel(str(data["level"])),
        defect_ids=tuple(data.get("defect_ids", ())),
        made_progress=data.get("made_progress"),
        text=str(data.get("text", "")),
    )


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def _collect_json_files(paths: Sequence[str]) -> list[Path]:
    """展开 CLI 传入的路径为 JSON 文件列表。"""
    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            candidate = Path.cwd() / path
            if candidate.exists():
                path = candidate
        if path.is_dir():
            resolved.extend(sorted(path.rglob("*.json")))
        elif path.is_file():
            resolved.append(path)
        else:
            print(f"[warn] 路径不存在，跳过: {raw}", file=sys.stderr)
    return resolved


def run_lint(args: argparse.Namespace) -> int:
    """用内置极简校验器检查 examples 与 schemas 的一致性。"""
    schema_dir = Path(args.schema_dir) if args.schema_dir else DEFAULT_SCHEMA_DIR
    if not schema_dir.is_dir():
        print(f"[skip] schema 目录不存在: {schema_dir}")
        return 0
    store = SchemaStore(schema_dir)
    if not store.names:
        print(f"[skip] schema 目录为空: {schema_dir}")
        return 0
    paths = args.paths or [str(EXAMPLES_DIR)]
    files = _collect_json_files(paths)
    checked = 0
    skipped: list[str] = []
    failures: list[tuple[Path, str, list[str]]] = []
    for file_path in files:
        document = _read_json(file_path)
        schema_name = _infer_schema_name(document, file_path.name)
        if schema_name is None or not store.has(schema_name):
            skipped.append(str(file_path))
            continue
        errors = _validate_document(document, schema_name, store)
        checked += 1
        if errors:
            failures.append((file_path, schema_name, errors))
        else:
            print(f"[ok] {file_path} <- {schema_name}")
    for file_path in skipped:
        print(f"[skip] {file_path}（无对应 schema 或文件类型为内部样例）")
    for file_path, schema_name, errors in failures:
        print(f"[FAIL] {file_path} <- {schema_name}")
        for error in errors:
            print(f"       {error}")
    print(
        f"lint 汇总：通过 {checked - len(failures)}/{checked}，"
        f"跳过 {len(skipped)}，失败 {len(failures)}"
    )
    return 1 if failures else 0


def _validate_document(document: Any, schema_name: str, store: SchemaStore) -> list[str]:
    """对已加载文档做 schema 校验。"""
    from .schema_lite import validate

    return validate(
        document,
        store.get(schema_name),
        store=store,
        schema_name=schema_name,
    )


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------


def run_grade(args: argparse.Namespace) -> int:
    """读取一个判分输入 JSON，输出 grade（或人工队列结论）。"""
    path = Path(args.input) if args.input else EXAMPLES_DIR / "grading-case.json"
    payload = _read_json(path)
    budget = Budget.from_dict(payload.get("budget", {}))
    usage = _budget_usage_from_dict(payload.get("usage", {}))
    defects = tuple(_defect_from_dict(item) for item in payload.get("defects", ()))
    rounds = tuple(_feedback_round_from_dict(item) for item in payload.get("feedback_rounds", ()))
    gates = tuple(
        HardGate(
            check_id=str(item["check_id"]),
            passed=bool(item["passed"]),
            evidence_uri=item.get("evidence_uri"),
        )
        for item in payload.get("hard_gates", ())
    )
    outcome = compute_grade(
        trial_id=str(payload["trial_id"]),
        defects=defects,
        hard_gates=gates,
        feedback_rounds=rounds,
        budget=budget,
        usage=usage,
        validity=Validity(str(payload.get("validity", "valid"))),
        cheat=bool(payload.get("cheat", False)),
        cheat_evidence=str(payload.get("cheat_evidence", "")),
        format_violation=bool(payload.get("format_violation", False)),
        format_violation_evidence=str(payload.get("format_violation_evidence", "")),
        manual_reason=str(payload.get("manual_reason", "")),
        feedback_blocked=bool(payload.get("feedback_blocked", False)),
        cannot_continue=bool(payload.get("cannot_continue", False)),
        judge_consistency_value=payload.get("judge_consistency"),
        judge_samples=int(payload.get("judge_samples", 0)),
        judge_manual_required=bool(payload.get("judge_manual_required", False)),
        decided_at=str(payload.get("decided_at", "1970-01-01T00:00:00Z")),
        rubric_version=str(payload.get("rubric_version", "1.0.0")),
        task_version=str(payload.get("task_version", "1.0.0")),
        boundary_review=bool(payload.get("boundary_review", True)),
    )
    if args.json:
        _print_json(outcome.to_dict())
    else:
        grade = outcome.grade
        if grade is None:
            print(f"trial {payload['trial_id']}: 转人工（{'; '.join(outcome.manual_reasons)}）")
        else:
            print(f"trial {payload['trial_id']}")
            print(f"  tier={grade.tier} tier_numeric={grade.tier_numeric} penalty={grade.penalty}")
            print(
                f"  rule={grade.decision.rule_id} status={grade.status} validity={grade.validity}"
            )
            print(f"  counted_in_leaderboard={grade.counted_in_leaderboard}")
            print(f"  flags={[str(flag) for flag in grade.flags]}")
            print(f"  cell={grade.penalty}/{grade.tier}({grade.cost.agent:.2f})")
            print(f"  rationale={grade.decision.rationale}")
            if outcome.manual_required:
                print(f"  manual_reasons={list(outcome.manual_reasons)}")
    return 0


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def run_scan(args: argparse.Namespace) -> int:
    """金丝雀跨 trial 扫描（examples/canary-trials.json）。"""
    path = Path(args.input) if args.input else EXAMPLES_DIR / "canary-trials.json"
    payload = _read_json(path)
    trials = payload["trials"] if isinstance(payload, dict) else payload
    allowed_hosts = list(args.allowed_host or ())
    if isinstance(payload, dict):
        allowed_hosts = list(payload.get("allowed_hosts", allowed_hosts))
    report = scan_documents(
        trials,
        token_owner=payload.get("registry") if isinstance(payload, dict) else None,
        allowed_hosts=allowed_hosts,
    )
    if args.json:
        _print_json(report.to_dict())
    else:
        print(report.to_markdown())
        print()
        _print_json(report.to_dict())
    return 1 if report.critical else 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _cells_from_dict(data: Mapping[str, Any]) -> dict[str, Cell]:
    """把 inherit 的 JSON 转为 Cell 字典。"""
    cells: dict[str, Cell] = {}
    for cell_id, raw in data.items():
        cells[str(cell_id)] = Cell(
            task_id=str(raw["task_id"]),
            model=str(raw["model"]),
            shell=str(raw.get("shell", "unknown")),
            status=CellStatus.PASS,
            tier=Tier(str(raw["tier"])),
            penalty=int(raw.get("penalty", 0)),
            cost_agent=float(raw.get("cost_agent", 0.0)),
        )
    return cells


def run_report(args: argparse.Namespace) -> int:
    """生成批次榜单报告。"""
    path = (
        Path(args.input) if args.input else EXAMPLES_DIR / "batch-2026-11-b1" / "batch-input.json"
    )
    payload = _read_json(path)
    trials = tuple(TrialResult.from_record(item) for item in payload.get("trials", ()))
    report = BatchReport(
        batch_id=str(payload["batch_id"]),
        trials=trials,
        inherited=_cells_from_dict(payload.get("inherit", {})),
        skips={str(key): str(value) for key, value in payload.get("skips", {}).items()},
        generated_at=payload.get("generated_at"),
    )
    if args.json:
        _print_json(report.to_dict())
    else:
        print(report.to_markdown())
    return 0


# ---------------------------------------------------------------------------
# simulate：端到端演示
# ---------------------------------------------------------------------------


def _build_demo_rubric() -> tuple[RubricSet, tuple[RubricItem, ...]]:
    """构造演示用 rubric：2 条 functional + 4 条 minor。"""
    items = (
        RubricItem(
            id="R1",
            severity=Severity.FUNCTIONAL,
            description="提交表单后出现成功结果面板",
            f1_symptom="填写表单点击提交后，页面没有任何结果提示",
            judge_question="提交表单后是否出现成功结果面板",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
        RubricItem(
            id="R2",
            severity=Severity.FUNCTIONAL,
            description="刷新页面后已提交的数据仍在",
            f1_symptom="刷新页面后刚才填写的内容全部消失",
            judge_question="刷新页面后已提交的数据是否仍然存在",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
        RubricItem(
            id="R3",
            severity=Severity.MINOR,
            description="按钮在窄屏下不换行",
            f1_symptom="窗口变窄时按钮文字挤在一起",
            judge_question="窄屏下按钮文字是否不重叠",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
        RubricItem(
            id="R4",
            severity=Severity.MINOR,
            description="错误提示文案语气一致",
            f1_symptom="错误提示有的很正式有的很随意",
            judge_question="错误提示文案语气是否一致",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
        RubricItem(
            id="R5",
            severity=Severity.MINOR,
            description="深色模式下对比度达标",
            f1_symptom="深色模式里部分文字看不清",
            judge_question="深色模式下文字与背景对比度是否达标",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
        RubricItem(
            id="R6",
            severity=Severity.MINOR,
            description="提交按钮有加载状态",
            f1_symptom="点击提交后按钮没有反应提示",
            judge_question="提交过程中按钮是否显示加载状态",
            kind=CheckKind.LLM_JUDGE,
            applies_to=("screenshot",),
        ),
    )
    rubric_set = RubricSet(task_id="web-001", items=items, rubric_version="1.0.0")
    return rubric_set, items


def _judge_for_scenario(items: Sequence[RubricItem], *, fixed: set[str]) -> JudgePanel:
    """构造离线评审团：已修复条目判 pass，其余 fail。"""

    def make_responder(should_pass: bool):
        def responder(prompt: str) -> str:
            item_id = prompt.split("rubric_item_id: ")[1].split("\n")[0].strip()
            verdict = should_pass and item_id in fixed
            return json.dumps({"verdict": bool(verdict)})

        return responder

    client_a = MockJudge(
        responder=make_responder(True), name="judge-a", provider="vendor-a", family="family-a"
    )
    client_b = MockJudge(
        responder=make_responder(True), name="judge-b", provider="vendor-b", family="family-b"
    )
    return JudgePanel(
        {"vendor-a": client_a, "vendor-b": client_b},
        target_model="demo-model",
        target_family="target-family",
        k=5,
    )


def run_simulate(args: argparse.Namespace) -> int:
    """端到端模拟一个 trial：状态机 + 反馈阶梯 + 判分 + 金丝雀（离线）。"""
    rubric_set, items = _build_demo_rubric()
    catalog = DefectCatalog.from_rubric_set(rubric_set)
    tracker = DefectTracker(catalog)
    ladder = FeedbackLadder()
    budget = Budget(max_rounds=6, max_feedback_rounds=3, max_cost_usd=15.0)
    usage = BudgetUsage()
    rng = random.Random(args.seed)
    trial_id = "web-001.demo-model.claude-code.e1.t01"
    machine = TrialMachine(trial_id)
    machine.apply("lease")
    machine.apply("start")

    # 场景：R1/R2 需要反馈后修复；R3-R6 是裁判发现的 minor，一直未修。
    auto_fixed = {"R1": FeedbackLevel.F1, "R2": FeedbackLevel.F1}
    received: dict[str, FeedbackLevel] = {}
    history: list[FeedbackRound] = []
    trace: list[str] = []
    round_no = 0
    terminal = None
    while round_no < budget.max_rounds:
        round_no += 1
        # Agent 按上一轮收到的反馈等级修复：达到所需等级即视为本轮已修复。
        failing = [
            item.id
            for item in items
            if not (
                item.id in auto_fixed
                and item.id in received
                and _level_rank(received[item.id]) >= _level_rank(auto_fixed[item.id])
            )
        ]
        machine.apply("stop")
        report = tracker.observe(round_no, failing)
        usage.add_round(tokens=50_000, agent_cost_usd=0.5, wall_clock_s=60.0)
        # 回填上一条反馈的效果
        if history and history[-1].made_progress is None:
            target = history[-1].defect_ids[0] if history[-1].defect_ids else ""
            history[-1].made_progress = target not in report.open
        gates_passed = not tracker.blocking_open()
        terminal = evaluate_terminal(
            hard_gates_passed=gates_passed,
            has_unresolved_blocking_defect=bool(tracker.blocking_open()),
            budget_exhausted=bool(usage.exhausted_reasons(budget)),
        )
        trace.append(
            f"round={round_no} event=observe new={len(report.new)} "
            f"fixed={len(report.fixed)} open={len(report.open)} "
            f"blocking_open={len(tracker.blocking_open())} terminal={None if terminal is None else terminal.code}"
        )
        if terminal is not None:
            break
        decision = ladder.next_decision(tracker, history, budget, usage)
        if decision is None:
            break
        machine.apply("needs_feedback")
        history.append(
            FeedbackRound(
                round_no=round_no,
                level=decision.level,
                defect_ids=(decision.defect_id,) if decision.defect_id else (),
                made_progress=None,
            )
        )
        if decision.defect_id:
            tracker.mark_feedback(decision.defect_id, decision.level)
        if decision.reason:
            trace.append(
                f"round={round_no} decision={decision.level} target={decision.defect_id} reason={decision.reason}"
            )
        usage.add_feedback_round(user_cost_usd=0.1)
        if decision.defect_id:
            received[decision.defect_id.split("#")[0]] = decision.level
        machine.apply("deliver")
    if machine.state == TrialState.OBSERVING or machine.state == TrialState.AWAITING_FEEDBACK:
        machine.apply("finish")
    machine.apply("score")

    fixed_final = {defect.rubric_item_id for defect in tracker.fixed_defects()}
    panel = _judge_for_scenario(items, fixed=fixed_final)
    panel_result = panel.evaluate_items(items)
    judge_members = list(panel_result.members)
    outcome = compute_grade(
        trial_id=trial_id,
        defects=tracker.defects,
        hard_gates=[HardGate(check_id="visible-tests", passed=not tracker.blocking_open())],
        feedback_rounds=history,
        budget=budget,
        usage=usage,
        validity=machine.validity,
        judge_consistency_value=panel_result.agreement,
        judge_samples=panel_result.samples,
        judge_manual_required=bool(panel_result.manual_queue),
        judge_panel=judge_members,
        decided_at="2026-11-06T00:00:00Z",
        boundary_review=False,
    )
    if outcome.grade is not None:
        record_grade(machine.record, outcome.grade)
    # 金丝雀演示：两个 trial 各自干净
    registry = CanaryRegistry()
    token_a = registry.register(trial_id)
    other_id = "web-001.other-model.claude-code.e1.t01"
    token_b = registry.register(other_id)
    canary_report = scan_documents(
        [
            CanaryTrial(
                trial_id=trial_id,
                token=token_a,
                artifacts={"src/main.py": f"# canary: {token_a}\nprint('ok')"},
                trace=[f"read src/main.py（{token_a}）"],
                expected_paths=("src/main.py",),
            ),
            CanaryTrial(
                trial_id=other_id,
                token=token_b,
                artifacts={"src/main.py": f"# canary: {token_b}\nprint('ok')"},
            ),
        ]
    )
    payload = {
        "trial_id": trial_id,
        "state": str(machine.state),
        "validity": str(machine.validity),
        "history": [event.to_dict() for event in machine.history],
        "rounds": round_no,
        "trace": trace,
        "defect_summary": tracker.summary(),
        "judge": panel_result.to_dict(),
        "canary": canary_report.to_dict(),
        "outcome": outcome.to_dict(),
        "cell": (
            None
            if outcome.grade is None
            else f"{outcome.grade.penalty}/{outcome.grade.tier}({outcome.grade.cost.agent:.2f})"
        ),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"simulate trial={trial_id} seed={args.seed}")
        for line in trace:
            print("  " + line)
        print(f"state={machine.state} validity={machine.validity} rounds={round_no}")
        print("defects: " + json.dumps(tracker.summary(), ensure_ascii=False))
        print(f"judge agreement={panel_result.agreement} manual_queue={panel_result.manual_queue}")
        if outcome.grade is None:
            print(f"grade: 转人工（{'; '.join(outcome.manual_reasons)}）")
        else:
            grade = outcome.grade
            print(
                f"grade: tier={grade.tier} penalty={grade.penalty} "
                f"rule={grade.decision.rule_id} flags={[str(flag) for flag in grade.flags]}"
            )
            print(f"cell: {payload['cell']}")
        print(
            f"canary: {'通过' if canary_report.passed else '未通过'}，critical={len(canary_report.critical)}"
        )
    _ = rng  # 预留随机种子（模拟抖动时使用）
    return 0


def _level_rank(level: FeedbackLevel) -> int:
    """反馈级别排序值。"""
    return {"F0": 0, "F1": 1, "F2": 2, "F3": 3}[str(level)]


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m evalspleef.core",
        description="LLM 自动化评测参考实现（Python 标准库）",
    )
    parser.add_argument("--version", action="version", version=f"evalspleef.core {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lint_parser = subparsers.add_parser("lint", help="用 schemas 校验 examples")
    lint_parser.add_argument("paths", nargs="*", help="文件或目录；缺省 reference/examples")
    lint_parser.add_argument(
        "--schema-dir", default=None, help="schema 目录（缺省 llm-eval/schemas）"
    )
    lint_parser.set_defaults(func=run_lint)

    grade_parser = subparsers.add_parser("grade", help="运行确定性档位聚合")
    grade_parser.add_argument("input", nargs="?", default=None, help="判分输入 JSON")
    grade_parser.add_argument("--json", action="store_true", help="输出 JSON")
    grade_parser.set_defaults(func=run_grade)

    scan_parser = subparsers.add_parser("scan", help="金丝雀跨 trial 扫描")
    scan_parser.add_argument("input", nargs="?", default=None, help="扫描输入 JSON")
    scan_parser.add_argument("--allowed-host", action="append", default=None, help="网络白名单主机")
    scan_parser.add_argument("--json", action="store_true", help="输出 JSON")
    scan_parser.set_defaults(func=run_scan)

    report_parser = subparsers.add_parser("report", help="生成批次榜单报告")
    report_parser.add_argument("input", nargs="?", default=None, help="批次输入 JSON")
    report_parser.add_argument("--json", action="store_true", help="输出 JSON")
    report_parser.set_defaults(func=run_report)

    simulate_parser = subparsers.add_parser("simulate", help="端到端模拟一个 trial")
    simulate_parser.add_argument("--seed", type=int, default=7, help="随机种子")
    simulate_parser.add_argument("--json", action="store_true", help="输出 JSON")
    simulate_parser.set_defaults(func=run_simulate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口（返回退出码）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
