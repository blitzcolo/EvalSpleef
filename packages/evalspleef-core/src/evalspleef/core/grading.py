"""evalspleef.core.grading — 扣分、档位算法与裁判采样一致性（docs/03 §9 的可执行版）。

事实来源：
- docs/03-grading.md §9（判定优先级表 P0-P14、档位算法、12 个边界用例）、§10（扣分）；
- schemas/grade.schema.json（grade 字段、rule_id/flags 枚举、allOf 约束）；
- appendix/grading-vectors.json（V01-V12 机器可读向量，本模块测试逐条覆盖）；
- docs/02-eval-flow.md §5.4/§6（成功条件与 P0-P5 优先级）。

判定优先级（§9.2，命中第一条即停止）
    P0 G-INVALID-00   validity=invalid                   无 grade
    P1 G-CHEAT-01     cheat_flag=true                    F, penalty=0
    P2 G-FORMAT-05    format_violation=true              F, penalty=0
    P3 G-HUMAN-99     validity=manual                    无 grade（人工）
    P4 G-F0-PERFECT   success, max_level=F0, total=0     P
    P5 G-F0-MINOR     success, max_level=F0, 只有 minor    A
    P6 G-F1-BPLUS     success, F1, multi=0, half_step     B+
    P7 G-F1-CPLUS     success, F1, multi>0, half_step     C+
    P8 G-F1-B         success, F1, multi=0, not half      B
    P9 G-F1-C         success, F1, multi>0, not half      C
    P10 G-F2-D        success, max_level=F2               D
    P11 G-F3-D        success, max_level=F3               D
    P12 G-BUDGET-F    not success, budget_exhausted       F
    P13 G-ABORT-04    not success, feedback_blocked/不可继续 F
    P14 G-ANOMALY-98  其余数据异常                        无 grade（转人工）

关键口径（docs/03 §9.1）
- success = hard_gates_passed 且 open_blocking=0；
- max_level 只看实际发出的 F1-F3（F0 催促与澄清不算）；
- need_feedback = feedback_rounds>=1 且已修复的缺陷数（半档分子）；
- multi_round_f1 = max_level_during_open=F1 且 feedback_rounds>=2 的已修复缺陷数；
- half_step = max_level=F1 且 3*need_feedback <= total_defects；
- 作弊/格式违规 penalty=0，但成绩有效且计入榜单（记 0）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .defects import DefectTracker
from .feedback import FeedbackRound
from .models import (
    ANOMALY_RULE_ID,
    INVALID_RULE_ID,
    Budget,
    BudgetUsage,
    Consistency,
    Cost,
    DecidedBy,
    Decision,
    Defect,
    DefectStatus,
    DefectSummary,
    FeedbackLevel,
    FeedbackSummary,
    Grade,
    GradeFlag,
    GradeStatus,
    HardGateSummary,
    JudgeMember,
    PenaltyItem,
    RuleId,
    Severity,
    Tier,
    Validity,
    max_feedback_level,
)

__all__ = [
    "JUDGE_CONSISTENCY_THRESHOLD",
    "GradeOutcome",
    "HardGate",
    "InvalidTrialNotGradable",
    "aggregate_judge_votes",
    "compute_grade",
    "compute_penalty",
    "defect_summary",
    "derive_inputs",
    "feedback_summary",
    "judge_consistency",
    "median_vote",
]

#: 裁判一致性低于该阈值转人工（docs/03 §11.3；具体阈值以标定结果为准）。
JUDGE_CONSISTENCY_THRESHOLD = 0.6


class InvalidTrialNotGradable(ValueError):
    """兼容异常：旧接口在 validity != valid 时抛出；现改为返回无 grade 的 outcome。"""


# ---------------------------------------------------------------------------
# 裁判采样：中位数与一致性（docs/03 §11.2）
# ---------------------------------------------------------------------------


def median_vote(votes: Sequence[bool]) -> bool:
    """二元结论的多数/中位数：奇数取中间；偶数平票按 fail 处理（保守）。"""
    if not votes:
        raise ValueError("没有可聚合的裁判投票")
    ordered = sorted(bool(vote) for vote in votes)
    middle = ordered[len(ordered) // 2]
    if len(ordered) % 2 == 1:
        return middle
    lower = ordered[len(ordered) // 2 - 1]
    return lower and middle


def judge_consistency(votes: Sequence[bool]) -> float:
    """一致率 = 与多数结论相同的票数 / 总票数（0..1）。"""
    if not votes:
        raise ValueError("没有可聚合的裁判投票")
    passed = sum(1 for vote in votes if vote)
    return max(passed, len(votes) - passed) / len(votes)


def aggregate_judge_votes(votes: Sequence[bool]) -> tuple[bool, float]:
    """返回 (中位结论, 一致率)。"""
    return median_vote(votes), judge_consistency(votes)


# ---------------------------------------------------------------------------
# 输入结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardGate:
    """一条 L1 硬门槛检查结果。"""

    check_id: str
    passed: bool
    evidence_uri: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {"check_id": self.check_id, "passed": self.passed}
        if self.evidence_uri is not None:
            payload["evidence_uri"] = self.evidence_uri
        return payload


@dataclass(frozen=True)
class GradeOutcome:
    """一次判定结果：产出 grade、或按规则不产出 grade（invalid/manual/anomaly）。

    derived 字段对应 docs/03 §9.1 的输入量，便于测试向量逐项断言。
    """

    grade: Grade | None
    rule_id: str | None
    manual_required: bool
    manual_reasons: tuple[str, ...] = ()
    hard_gates_passed: bool = False
    total_items: int = 0
    tier: Tier | None = None
    penalty: int | None = None
    flags: tuple[GradeFlag, ...] = ()
    counted_in_leaderboard: bool = False
    derived: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "grade": None if self.grade is None else self.grade.to_dict(),
            "rule_id": self.rule_id,
            "manual_required": self.manual_required,
            "manual_reasons": list(self.manual_reasons),
            "hard_gates_passed": self.hard_gates_passed,
            "total_items": self.total_items,
            "tier": None if self.tier is None else str(self.tier),
            "penalty": self.penalty,
            "flags": [str(flag) for flag in self.flags],
            "counted_in_leaderboard": self.counted_in_leaderboard,
            "derived": dict(self.derived),
        }


# ---------------------------------------------------------------------------
# 扣分与派生量（docs/03 §9.1 / §10.1）
# ---------------------------------------------------------------------------


def _unique_defects(defects: Iterable[Defect]) -> list[Defect]:
    """按 defect_id 去重并排序（同一轮重复上报不重复计分）。"""
    by_id: dict[str, Defect] = {}
    for defect in defects:
        if defect.defect_id in by_id:
            raise ValueError(f"判决输入出现重复 defect_id: {defect.defect_id}")
        by_id[defect.defect_id] = defect
    return sorted(by_id.values(), key=lambda item: (item.first_round, item.defect_id))


def _severity_weight(severity: Severity) -> int:
    """severity -> 权重 1/3/5（00-conventions §4.4）。"""
    return {"minor": 1, "functional": 3, "fatal": 5}[str(severity)]


def compute_penalty(defects: Iterable[Defect]) -> tuple[int, list[PenaltyItem]]:
    """扣分：每个缺陷记录在首次发现轮计一次权重，persisting 不重复计。

    Returns:
        (penalty_total, penalty_breakdown)，与 grade.schema.json 对齐。
    """
    total = 0
    breakdown: list[PenaltyItem] = []
    for defect in _unique_defects(defects):
        weight = _severity_weight(defect.severity)
        total += weight
        breakdown.append(
            PenaltyItem(
                defect_id=defect.defect_id,
                rubric_item_id=defect.rubric_item_id,
                severity=defect.severity,
                weight=weight,
                first_round=defect.first_round,
                counted=True,
            )
        )
    return total, breakdown


def _fixed_with_feedback(defects: Sequence[Defect]) -> list[Defect]:
    """已修复且收到过至少 1 轮反馈的缺陷（need_feedback 的定义）。"""
    return [
        defect
        for defect in defects
        if defect.status == DefectStatus.FIXED and defect.feedback_rounds >= 1
    ]


def _multi_round_f1(defects: Sequence[Defect]) -> list[Defect]:
    """max_level_during_open=F1 且 feedback_rounds>=2 的已修复缺陷。"""
    return [
        defect
        for defect in defects
        if defect.status == DefectStatus.FIXED
        and defect.max_level_during_open == FeedbackLevel.F1
        and defect.feedback_rounds >= 2
    ]


def defect_summary(defects: Iterable[Defect]) -> DefectSummary:
    """生成 grade.schema.json $defs.defect_summary。"""
    items = _unique_defects(defects)
    by_severity = {"minor": 0, "functional": 0, "fatal": 0}
    for defect in items:
        by_severity[str(defect.severity)] += 1
    open_blocking = sum(1 for defect in items if defect.is_blocking)
    fixed = sum(1 for defect in items if defect.status == DefectStatus.FIXED)
    return DefectSummary(
        total=len(items),
        by_severity=by_severity,
        open_blocking=open_blocking,
        fixed=fixed,
        requiring_feedback=len(_fixed_with_feedback(items)),
        multi_round_f1=len(_multi_round_f1(items)),
        regressed=_regressed_count(items),
    )


def _regressed_count(defects: Sequence[Defect]) -> int:
    """修复后复发的缺陷数：同一 rubric item 的后续缺陷，且此前有已修复缺陷。"""
    by_item: dict[str, list[Defect]] = {}
    for defect in defects:
        by_item.setdefault(defect.rubric_item_id, []).append(defect)
    regressed = 0
    for series in by_item.values():
        fixed_before = False
        for defect in sorted(series, key=lambda item: item.first_round):
            if fixed_before and defect.first_round > 1:
                regressed += 1
            if defect.status == DefectStatus.FIXED:
                fixed_before = True
    return regressed


def _real_feedback(rounds: Sequence[FeedbackRound]) -> list[FeedbackRound]:
    """只保留 F1-F3 的实质反馈（F0 催促不计）。"""
    return [
        entry
        for entry in rounds
        if entry.level in (FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F3)
    ]


def feedback_summary(
    defects: Iterable[Defect],
    rounds: Sequence[FeedbackRound] = (),
    *,
    nudges_used: int = 0,
) -> FeedbackSummary:
    """生成 grade.schema.json $defs.feedback_summary。"""
    items = _unique_defects(defects)
    real = _real_feedback(rounds)
    level_counts = {"F0": 0, "F1": 0, "F2": 0, "F3": 0}
    for entry in rounds:
        level_counts[str(entry.level)] += 1
    level_counts["F0"] = max(level_counts["F0"], nudges_used)
    top = max_feedback_level([entry.level for entry in real]) or FeedbackLevel.F0
    total_defects = len(items)
    need_feedback = len(_fixed_with_feedback(items))
    half_step = top == FeedbackLevel.F1 and total_defects > 0 and 3 * need_feedback <= total_defects
    return FeedbackSummary(
        max_level=top,
        feedback_rounds=len(real),
        level_counts=level_counts,
        nudges_used=nudges_used,
        total_defects=total_defects,
        need_feedback=need_feedback,
        half_step_applied=half_step,
        multi_round_f1=len(_multi_round_f1(items)),
    )


def derive_inputs(
    *,
    defects: Iterable[Defect],
    hard_gates_passed: bool,
    rounds: Sequence[FeedbackRound] = (),
    nudges_used: int = 0,
) -> dict[str, Any]:
    """按 docs/03 §9.1 计算测试向量里的 derived 字段（供断言）。"""
    items = _unique_defects(defects)
    real = _real_feedback(rounds)
    top = max_feedback_level([entry.level for entry in real]) or FeedbackLevel.F0
    open_blocking = sum(1 for defect in items if defect.is_blocking)
    success = bool(hard_gates_passed) and open_blocking == 0
    need_feedback = len(_fixed_with_feedback(items))
    half_step = top == FeedbackLevel.F1 and len(items) > 0 and 3 * need_feedback <= len(items)
    return {
        "max_level": str(top),
        "success": success,
        "multi_round_f1": len(_multi_round_f1(items)),
        "need_feedback": need_feedback,
        "total_defects": len(items),
        "half_step": half_step,
        "open_blocking": open_blocking,
        "feedback_rounds": len(real),
        "nudges_used": nudges_used,
    }


# ---------------------------------------------------------------------------
# 档位规则（docs/03 §9.2）
# ---------------------------------------------------------------------------


def _decision(rule_id: RuleId, rationale: str) -> Decision:
    """按规则 ID 构造 Decision（优先级来自 RULE_PRIORITIES）。"""
    return Decision.for_rule(rule_id, rationale)


def _rationale(derived: dict[str, Any], suffix: str = "") -> str:
    """生成 8 字以上的可审计理由（含关键输入量）。"""
    base = (
        f"hard_gates_passed={not derived.get('_gates_failed', False)}, "
        f"open_blocking={derived['open_blocking']}, "
        f"max_level={derived['max_level']}, "
        f"multi_round_f1={derived['multi_round_f1']}, "
        f"need_feedback={derived['need_feedback']}/{derived['total_defects']}, "
        f"half_step={derived['half_step']}"
    )
    return base + (f"；{suffix}" if suffix else "")


def _flags_for(
    *,
    rule_id: RuleId | str,
    derived: dict[str, Any],
) -> tuple[GradeFlag, ...]:
    """按命中的规则生成 grade.flags（与 docs/03 §9.2 的关键 flags 列一致）。"""
    flags: list[GradeFlag] = []
    if rule_id == RuleId.CHEAT_01:
        flags.extend([GradeFlag.CHEAT, GradeFlag.ZEROED])
    if rule_id == RuleId.FORMAT_05:
        flags.extend([GradeFlag.FORMAT_VIOLATION, GradeFlag.ZEROED])
    if rule_id == RuleId.F0_MINOR:
        flags.append(GradeFlag.MINOR_ONLY)
    if rule_id == RuleId.F1_BPLUS:
        flags.append(GradeFlag.HALF_STEP)
    if rule_id == RuleId.F1_CPLUS:
        flags.extend([GradeFlag.F1_MULTIROUND, GradeFlag.HALF_STEP])
    if rule_id == RuleId.F1_C:
        flags.append(GradeFlag.F1_MULTIROUND)
    if rule_id == RuleId.BUDGET_F:
        flags.append(GradeFlag.BUDGET_EXHAUSTED)
        if derived.get("_gates_failed"):
            flags.append(GradeFlag.HARD_GATE_FAILED)
        if derived.get("_feedback_blocked"):
            flags.append(GradeFlag.FEEDBACK_BLOCKED)
        if derived.get("_nudge_capped"):
            flags.append(GradeFlag.NUDGE_CAPPED)
    if rule_id == RuleId.ABORT_04:
        flags.append(GradeFlag.TERMINATED_EARLY)
        if derived.get("_feedback_blocked"):
            flags.append(GradeFlag.FEEDBACK_BLOCKED)
    if derived.get("_judge_low_agreement"):
        flags.append(GradeFlag.LOW_JUDGE_AGREEMENT)
    return tuple(dict.fromkeys(flags))


def _cost_from_usage(usage: BudgetUsage, pricing_table_version: str | None) -> Cost:
    """从 BudgetUsage 构造 grade.cost。"""
    return Cost(
        agent=usage.agent_cost_usd,
        judge=usage.judge_cost_usd,
        user=usage.user_cost_usd,
        pricing_table_version=pricing_table_version,
    )


def compute_grade(
    *,
    trial_id: str,
    defects: Iterable[Defect],
    hard_gates: Sequence[HardGate],
    feedback_rounds: Sequence[FeedbackRound] = (),
    budget: Budget | None = None,
    usage: BudgetUsage | None = None,
    validity: Validity = Validity.VALID,
    cheat: bool = False,
    cheat_evidence: str = "",
    format_violation: bool = False,
    format_violation_evidence: str = "",
    feedback_blocked: bool = False,
    cannot_continue: bool = False,
    judge_consistency_value: float | None = None,
    judge_samples: int = 0,
    judge_manual_required: bool = False,
    judge_panel: Sequence[JudgeMember] = (),
    decided_at: str = "1970-01-01T00:00:00Z",
    rubric_version: str = "1.0.0",
    task_version: str = "1.0.0",
    pricing_table_version: str | None = None,
    manual_reason: str = "",
    boundary_review: bool = False,
) -> GradeOutcome:
    """按 docs/03 §9.2 优先级表计算一个 trial 的 grade（或按规则不产出 grade）。

    Args:
        validity: trial.validity；invalid 不产出 grade（P0），manual 转人工（P3）。
        cheat: 作弊命中（P1）：F + penalty=0 + flags{cheat,zeroed}，成绩有效计入榜单。
        format_violation: 逻辑题格式违规（P2）：F + penalty=0 + flags{format_violation,zeroed}。
        feedback_blocked: 泄露重写 3 次失败（P13，G-ABORT-04）。
        cannot_continue: P4 不可继续（P13）。
        judge_manual_required/judge_consistency_value: 裁判不可用/低一致性 -> P3 人工。
        boundary_review: 预留的 P/F 边界档人工复核开关（默认关闭；vectors 期望
            requires_human=false，详见 docs/03 §9.7）。
    """
    budget = budget or Budget()
    usage = usage or BudgetUsage()
    defect_items = _unique_defects(defects)
    gates_passed = all(gate.passed for gate in hard_gates)
    d_summary = defect_summary(defect_items)
    f_summary = feedback_summary(defect_items, feedback_rounds, nudges_used=usage.nudges)
    derived = derive_inputs(
        defects=defect_items,
        hard_gates_passed=gates_passed,
        rounds=feedback_rounds,
        nudges_used=usage.nudges,
    )
    exhausted = bool(usage.exhausted_reasons(budget))
    judge_low = (
        judge_consistency_value is not None
        and judge_consistency_value < JUDGE_CONSISTENCY_THRESHOLD
    )
    context: dict[str, Any] = {
        **derived,
        "_gates_failed": not gates_passed,
        "_feedback_blocked": feedback_blocked,
        "_cannot_continue": cannot_continue,
        "_nudge_capped": usage.nudges >= budget.max_nudges,
        "_judge_low_agreement": judge_low,
        "_exhausted": exhausted,
    }
    hard_gate_summary = HardGateSummary(
        passed=gates_passed,
        total=len(hard_gates),
        failed=tuple(gate.check_id for gate in hard_gates if not gate.passed),
        evidence_uri=next((gate.evidence_uri for gate in hard_gates if gate.evidence_uri), None),
    )
    cost = _cost_from_usage(usage, pricing_table_version)
    consistency = Consistency(
        metric="percent_agreement",
        value=judge_consistency_value,
        samples=judge_samples,
        threshold=(JUDGE_CONSISTENCY_THRESHOLD if judge_consistency_value is not None else None),
        note=None if judge_consistency_value is None else "评审团逐条采样一致率",
    )

    def _manual_outcome(
        rule_id: str,
        reasons: Iterable[str],
        flags: tuple[GradeFlag, ...],
    ) -> GradeOutcome:
        """构造不产出 grade 的 outcome。"""
        return GradeOutcome(
            grade=None,
            rule_id=rule_id,
            manual_required=True,
            manual_reasons=tuple(reason for reason in reasons if reason),
            hard_gates_passed=gates_passed,
            total_items=len(defect_items),
            tier=None,
            penalty=None,
            flags=flags,
            counted_in_leaderboard=False,
            derived=dict(derived),
        )

    # P0：invalid 不产出 grade（不计榜，重跑）。
    if validity == Validity.INVALID:
        return GradeOutcome(
            grade=None,
            rule_id=INVALID_RULE_ID,
            manual_required=False,
            manual_reasons=(manual_reason or "validity=invalid，不产出 grade",),
            hard_gates_passed=gates_passed,
            total_items=len(defect_items),
            tier=None,
            penalty=None,
            flags=(GradeFlag.INVALID,),
            counted_in_leaderboard=False,
            derived=dict(derived),
        )
    # P3：manual 转人工（人工/仲裁产出）。
    if validity == Validity.MANUAL:
        return _manual_outcome(
            str(RuleId.HUMAN_99),
            (manual_reason or "validity=manual，转人工仲裁",),
            (GradeFlag.MANUAL_ARBITRATION,),
        )
    if validity == Validity.PENDING:
        return _manual_outcome(
            ANOMALY_RULE_ID,
            (manual_reason or "validity=pending，数据未齐不产出 grade",),
            (GradeFlag.TERMINATED_EARLY,),
        )
    # 裁判不可用/低一致性 -> P3 人工（不静默记 0）。
    if judge_manual_required or judge_low:
        reasons = []
        if judge_manual_required:
            reasons.append("裁判超时/输出无效，转人工（docs/02 §8）")
        if judge_low:
            reasons.append(
                f"裁判一致性 {judge_consistency_value:.2f} 低于阈值 {JUDGE_CONSISTENCY_THRESHOLD}"
            )
        return _manual_outcome(
            str(RuleId.HUMAN_99),
            reasons,
            (
                *_flags_for(rule_id=str(RuleId.HUMAN_99), derived=context),
                GradeFlag.MANUAL_ARBITRATION,
            ),
        )
    if boundary_review and not cheat and not format_violation and derived["success"]:
        # 可选：P/F 边界档人工复核（默认关闭，避免与 vectors 的 requires_human=false 冲突）。
        pass

    # 逐条匹配优先级（P1 -> P13）。
    decision: Decision
    zero_penalty = False
    if cheat:
        decision = _decision(
            RuleId.CHEAT_01,
            _rationale(
                context, f"作弊命中：判 0 并标记 cheat；证据={cheat_evidence or '见隔离报告'}"
            ),
        )
        zero_penalty = True
    elif format_violation:
        decision = _decision(
            RuleId.FORMAT_05,
            _rationale(
                context,
                f"逻辑题格式违规：判 0 并标记；证据={format_violation_evidence or '见产物审计'}",
            ),
        )
        zero_penalty = True
    elif not derived["success"]:
        if exhausted:
            decision = _decision(
                RuleId.BUDGET_F,
                _rationale(context, "预算耗尽且未达到成功条件，判 Failed"),
            )
        elif feedback_blocked or cannot_continue:
            decision = _decision(
                RuleId.ABORT_04,
                _rationale(context, "反馈无法生成/不可继续，判 Failed（G-ABORT-04）"),
            )
        else:
            return _manual_outcome(
                ANOMALY_RULE_ID,
                (
                    "数据异常：未达成功条件、预算未耗尽、也未 abort（不应出现的状态），"
                    "按 docs/03 §9.2 P14 转人工",
                ),
                (GradeFlag.TERMINATED_EARLY,),
            )
    elif derived["max_level"] == FeedbackLevel.F0 and any(
        defect.severity in (Severity.FUNCTIONAL, Severity.FATAL) for defect in defect_items
    ):
        return _manual_outcome(
            ANOMALY_RULE_ID,
            ("数据异常：success 且 max_level=F0 但存在 blocking 缺陷记录（违反 §9.8 I2），转人工",),
            (GradeFlag.TERMINATED_EARLY,),
        )
    elif derived["max_level"] == FeedbackLevel.F0:
        if derived["total_defects"] == 0:
            decision = _decision(RuleId.F0_PERFECT, _rationale(context, "一次过且零缺陷"))
        else:
            decision = _decision(
                RuleId.F0_MINOR, _rationale(context, "未发实质反馈，只剩 minor 缺陷")
            )
    elif derived["max_level"] == FeedbackLevel.F1:
        multi = derived["multi_round_f1"]
        half = derived["half_step"]
        if multi > 0:
            decision = _decision(
                RuleId.F1_CPLUS if half else RuleId.F1_C,
                _rationale(context, "存在多轮 F1 才修复的缺陷"),
            )
        else:
            decision = _decision(
                RuleId.F1_BPLUS if half else RuleId.F1_B,
                _rationale(context, "单轮 F1 修复"),
            )
    elif derived["max_level"] == FeedbackLevel.F2:
        decision = _decision(RuleId.F2_D, _rationale(context, "用到 F2，最高 D"))
    else:
        decision = _decision(RuleId.F3_D, _rationale(context, "用到 F3，最高 D"))

    tier = {
        RuleId.F0_PERFECT: Tier.P,
        RuleId.F0_MINOR: Tier.A,
        RuleId.F1_BPLUS: Tier.B_PLUS,
        RuleId.F1_B: Tier.B,
        RuleId.F1_CPLUS: Tier.C_PLUS,
        RuleId.F1_C: Tier.C,
        RuleId.F2_D: Tier.D,
        RuleId.F3_D: Tier.D,
        RuleId.CHEAT_01: Tier.F,
        RuleId.FORMAT_05: Tier.F,
        RuleId.BUDGET_F: Tier.F,
        RuleId.ABORT_04: Tier.F,
        RuleId.HUMAN_99: Tier.F,
    }[decision.rule_id]
    penalty, breakdown = compute_penalty(defect_items)
    if zero_penalty:
        penalty = 0
        breakdown = [
            PenaltyItem(
                defect_id=item.defect_id,
                rubric_item_id=item.rubric_item_id,
                severity=item.severity,
                weight=_severity_weight(item.severity),
                first_round=item.first_round,
                counted=False,
            )
            for item in defect_items
        ]
    flags = _flags_for(rule_id=decision.rule_id, derived=context)
    grade = Grade(
        trial_id=trial_id,
        tier=tier,
        decision=decision,
        decided_at=decided_at,
        hard_gate_summary=hard_gate_summary,
        defect_summary=d_summary,
        feedback_summary=f_summary,
        cost=cost,
        consistency=consistency,
        penalty=penalty,
        status=GradeStatus.FINAL,
        validity=Validity.VALID,
        flags=flags,
        decided_by=DecidedBy.AUTO,
        penalty_breakdown=tuple(breakdown),
        manual_notes=None,
        rubric_version=rubric_version,
        task_version=task_version,
        judge_panel=tuple(judge_panel),
        defects=tuple(item.to_summary_item() for item in defect_items),
    )
    return GradeOutcome(
        grade=grade,
        rule_id=str(decision.rule_id),
        manual_required=False,
        manual_reasons=(),
        hard_gates_passed=gates_passed,
        total_items=len(defect_items),
        tier=tier,
        penalty=penalty,
        flags=flags,
        counted_in_leaderboard=grade.counted_in_leaderboard,
        derived=dict(derived),
    )


def summarize_tracker(tracker: DefectTracker) -> dict[str, Any]:
    """把 DefectTracker 快照整理成判分输入摘要（CLI/报告用）。"""
    defects = tracker.defects
    return {
        "defect_summary": defect_summary(defects).to_dict(),
        "defects": [defect.to_dict() for defect in defects],
    }
