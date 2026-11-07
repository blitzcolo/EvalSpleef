"""evalspleef.core.models — 跨文档共享数据契约的 Python 镜像。

事实来源（出现不一致时以文档/Schema 为准，并同步本模块）：
- docs/00-conventions.md §4：枚举拼写、预算字段名、档位数值映射、扣分权重；
- docs/02-eval-flow.md：trial 生命周期、终态优先级、费用口径；
- schemas/rubric.schema.json、defect.schema.json、feedback.schema.json、grade.schema.json：
  判定体系的字段契约（由 grading 队友冻结）。

设计约定
--------
- 只用 Python 标准库；
- 枚举值一律小写英文（档位/反馈级别/规则 ID 除外），与 schema enum 逐字一致；
- to_dict() 只输出 schema 已定义的字段，可选字段为空时省略（additionalProperties=false）；
- 时间字段用 ISO-8601 UTC 字符串，不引入第三方日期库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "ANOMALY_RULE_ID",
    "BOUNDARY_TIERS",
    "CELL_FORMAT",
    "DEFAULT_DISPOSITIONS",
    "DISPOSITION_VALIDITY",
    "FEEDBACK_LEVEL_ORDER",
    "INVALID_RULE_ID",
    "PENALTY_WEIGHT",
    "RULE_PRIORITIES",
    "TIER_DISPLAY",
    "TIER_ORDER",
    "TIER_VALUE",
    "Budget",
    "BudgetUsage",
    "CellStatus",
    "CheckKind",
    "Consistency",
    "Cost",
    "DecidedBy",
    "Decision",
    "Defect",
    "DefectProgress",
    "DefectStatus",
    "DefectSummary",
    "Disposition",
    "ErrorClass",
    "EscalationPolicy",
    "Evidence",
    "EvidenceKind",
    "FeedbackLevel",
    "FeedbackSummary",
    "Grade",
    "GradeFlag",
    "GradeStatus",
    "HardGateSummary",
    "HumanOverride",
    "JudgeMember",
    "ObservationSource",
    "PenaltyItem",
    "RubricItem",
    "RubricSet",
    "RuleId",
    "Severity",
    "Tier",
    "TrialError",
    "TrialId",
    "TrialRecord",
    "TrialState",
    "Validity",
    "batch_id_is_valid",
    "defect_id_for",
    "display_tier",
    "format_trial_id",
    "max_feedback_level",
    "median_tier",
    "parse_batch_id",
    "parse_defect_id",
    "parse_trial_id",
    "tier_by_value",
    "tier_value",
    "to_plain",
]


class _StrEnum(str, Enum):
    """与 JSON Schema enum 对齐的字符串枚举基类。"""

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}.{self.name}"


# ---------------------------------------------------------------------------
# 00-conventions 第 4 节：共享枚举（拼写不得修改）
# ---------------------------------------------------------------------------


class TrialState(_StrEnum):
    """trial 生命周期状态（00-conventions §4.1，docs/02 §4）。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    OBSERVING = "observing"
    AWAITING_FEEDBACK = "awaiting_feedback"
    VERIFYING = "verifying"
    SCORED = "scored"
    INVALID = "invalid"
    PAUSED_MANUAL = "paused_manual"
    FAILED_INFRA = "failed_infra"


class FeedbackLevel(_StrEnum):
    """反馈级别（00-conventions §4.2）。F0 最低，F3 最高。"""

    F0 = "F0"
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"


class Tier(_StrEnum):
    """档位（00-conventions §4.3），枚举按优劣排序。"""

    P = "P"
    A = "A"
    B_PLUS = "B+"
    B = "B"
    C_PLUS = "C+"
    C = "C"
    D = "D"
    F = "F"


class Severity(_StrEnum):
    """缺陷严重度（00-conventions §4.4）。"""

    MINOR = "minor"
    FUNCTIONAL = "functional"
    FATAL = "fatal"


class CheckKind(_StrEnum):
    """检查类型（00-conventions §4.5）。"""

    SCRIPT = "script"
    LLM_JUDGE = "llm_judge"
    AGENT_JUDGE = "agent_judge"
    HUMAN = "human"


class ErrorClass(_StrEnum):
    """错误分类（00-conventions §4.6）。"""

    PROVIDER = "provider"
    MODEL = "model"
    CHEAT = "cheat"
    ISOLATION = "isolation"
    ENVIRONMENT = "environment"
    VERIFICATION = "verification"
    JUDGE = "judge"
    VERSION = "version"


class Disposition(_StrEnum):
    """错误处置（00-conventions §4.6）。"""

    RETRY = "retry"
    SCORE = "score"
    ZERO = "zero"
    MANUAL = "manual"
    DISCARD = "discard"


class Validity(_StrEnum):
    """成绩有效性（00-conventions §4.7）。"""

    VALID = "valid"
    INVALID = "invalid"
    PENDING = "pending"
    MANUAL = "manual"


class DefectStatus(_StrEnum):
    """缺陷跨轮状态（defect.schema.json $defs.defect_status）。"""

    NEW = "new"
    OPEN = "open"
    PERSISTING = "persisting"
    FIXED = "fixed"


class DefectProgress(_StrEnum):
    """相对上一轮的进展（defect.schema.json observation_item.progress）。"""

    NONE = "none"
    PARTIAL = "partial"
    FIXED = "fixed"


class EvidenceKind(_StrEnum):
    """证据类型（defect.schema.json $defs.evidence）。"""

    SCREENSHOT = "screenshot"
    LOG_EXCERPT = "log_excerpt"
    FILE_PATH = "file_path"
    COMMAND_OUTPUT = "command_output"
    VIDEO_FRAME = "video_frame"
    DIFF_HUNK = "diff_hunk"
    OTHER = "other"


class ObservationSource(_StrEnum):
    """一轮观察结论的来源（defect.schema.json）。"""

    SCRIPT = "script"
    LLM_JUDGE = "llm_judge"
    AGENT_JUDGE = "agent_judge"
    HUMAN = "human"
    MIXED = "mixed"


class CellStatus(_StrEnum):
    """榜单单元格状态（docs/02 §10：Pending/Running/Scored/Pass/Skip/Manual）。"""

    PENDING = "pending"
    RUNNING = "running"
    SCORED = "scored"
    PASS = "pass"
    SKIP = "skip"
    MANUAL = "manual"


class DecidedBy(_StrEnum):
    """grade 判定者（grade.schema.json $defs 属性 decided_by）。"""

    AUTO = "auto"
    HUMAN = "human"
    ARBITER = "arbiter"


class GradeStatus(_StrEnum):
    """grade 状态（grade.schema.json）。"""

    FINAL = "final"
    PROVISIONAL = "provisional"
    SUPERSEDED = "superseded"


class GradeFlag(_StrEnum):
    """判定标记（grade.schema.json $defs.flag，17 个）。"""

    CHEAT = "cheat"
    ZEROED = "zeroed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARD_GATE_FAILED = "hard_gate_failed"
    FEEDBACK_BLOCKED = "feedback_blocked"
    TERMINATED_EARLY = "terminated_early"
    INVALID = "invalid"
    MINOR_ONLY = "minor_only"
    F1_MULTIROUND = "f1_multiround"
    HALF_STEP = "half_step"
    NUDGE_CAPPED = "nudge_capped"
    LOW_JUDGE_AGREEMENT = "low_judge_agreement"
    MANUAL_ARBITRATION = "manual_arbitration"
    LEAK_INCIDENT = "leak_incident"
    FORMAT_VIOLATION = "format_violation"
    GUESS_SUSPECT = "guess_suspect"
    HUMAN_OVERRIDE = "human_override"


class RuleId(_StrEnum):
    """档位判定规则 ID（grade.schema.json $defs.rule_id，与 docs/03 §9.2 一致）。

    G-INVALID-00 与 G-ANOMALY-98 不产生 grade 记录，因此不在 Grade.decision 的
    枚举里；由 grading.compute_grade 的 GradeOutcome 直接返回字符串规则 ID。
    """

    CHEAT_01 = "G-CHEAT-01"
    FORMAT_05 = "G-FORMAT-05"
    HUMAN_99 = "G-HUMAN-99"
    F0_PERFECT = "G-F0-PERFECT"
    F0_MINOR = "G-F0-MINOR"
    F1_B = "G-F1-B"
    F1_BPLUS = "G-F1-BPLUS"
    F1_C = "G-F1-C"
    F1_CPLUS = "G-F1-CPLUS"
    F2_D = "G-F2-D"
    F3_D = "G-F3-D"
    BUDGET_F = "G-BUDGET-F"
    ABORT_04 = "G-ABORT-04"


CELL_STATUS_DISPLAY: dict[CellStatus, str] = {
    CellStatus.PENDING: "Pending",
    CellStatus.RUNNING: "Running",
    CellStatus.SCORED: "Scored",
    CellStatus.PASS: "Pass",
    CellStatus.SKIP: "Skip",
    CellStatus.MANUAL: "Manual",
}

# ---------------------------------------------------------------------------
# 档位、扣分、反馈常量（00-conventions §4.3 / §4.4）
# ---------------------------------------------------------------------------

#: 档位从优到劣的顺序，索引可用于排序。
TIER_ORDER: tuple[Tier, ...] = (
    Tier.P,
    Tier.A,
    Tier.B_PLUS,
    Tier.B,
    Tier.C_PLUS,
    Tier.C,
    Tier.D,
    Tier.F,
)

#: 聚合器数值映射：P=7 ... F=0（00-conventions §4.3）。
TIER_VALUE: dict[Tier, int] = {tier: 7 - index for index, tier in enumerate(TIER_ORDER)}

#: 榜单展示文案（00-conventions §4.3）。
TIER_DISPLAY: dict[Tier, str] = {
    Tier.P: "Perfect",
    Tier.A: "A",
    Tier.B_PLUS: "B+",
    Tier.B: "B",
    Tier.C_PLUS: "C+",
    Tier.C: "C",
    Tier.D: "D",
    Tier.F: "Failed",
}

#: 边界档：Perfect 与 Failed 是人工 L3 复核对象（plan-v1 §4.1）。
BOUNDARY_TIERS: frozenset[Tier] = frozenset({Tier.P, Tier.F})

#: 缺陷扣分权重（00-conventions §4.4）。
PENALTY_WEIGHT: dict[Severity, int] = {
    Severity.MINOR: 1,
    Severity.FUNCTIONAL: 3,
    Severity.FATAL: 5,
}

#: 反馈级别从低到高。
FEEDBACK_LEVEL_ORDER: tuple[FeedbackLevel, ...] = (
    FeedbackLevel.F0,
    FeedbackLevel.F1,
    FeedbackLevel.F2,
    FeedbackLevel.F3,
)

#: 判定规则优先级（docs/03-grading.md §9.2 表 P0-P14，数字与表一致）。
RULE_PRIORITIES: dict[RuleId, int] = {
    RuleId.CHEAT_01: 1,
    RuleId.FORMAT_05: 2,
    RuleId.HUMAN_99: 3,
    RuleId.F0_PERFECT: 4,
    RuleId.F0_MINOR: 5,
    RuleId.F1_BPLUS: 6,
    RuleId.F1_CPLUS: 7,
    RuleId.F1_B: 8,
    RuleId.F1_C: 9,
    RuleId.F2_D: 10,
    RuleId.F3_D: 11,
    RuleId.BUDGET_F: 12,
    RuleId.ABORT_04: 13,
}

#: 不产出 grade 的规则 ID（docs/03 §9.2 的 P0/P3/P14）。
INVALID_RULE_ID = "G-INVALID-00"
ANOMALY_RULE_ID = "G-ANOMALY-98"

#: 错误类别 -> 默认处置（drafts/plan-v1.md §8 与 Harbor 错误策略表）。
DEFAULT_DISPOSITIONS: dict[ErrorClass, Disposition] = {
    ErrorClass.PROVIDER: Disposition.RETRY,
    ErrorClass.MODEL: Disposition.SCORE,
    ErrorClass.CHEAT: Disposition.ZERO,
    ErrorClass.ISOLATION: Disposition.DISCARD,
    ErrorClass.ENVIRONMENT: Disposition.RETRY,
    ErrorClass.VERIFICATION: Disposition.RETRY,
    ErrorClass.JUDGE: Disposition.MANUAL,
    ErrorClass.VERSION: Disposition.SCORE,
}

#: 处置 -> trial 成绩有效性。
DISPOSITION_VALIDITY: dict[Disposition, Validity] = {
    Disposition.RETRY: Validity.INVALID,
    Disposition.SCORE: Validity.VALID,
    Disposition.ZERO: Validity.VALID,
    Disposition.MANUAL: Validity.MANUAL,
    Disposition.DISCARD: Validity.INVALID,
}

#: 榜单单元格格式（00-conventions §4.4）：扣分/档位(费用,2 位小数)。
CELL_FORMAT = "{penalty}/{tier}({cost:.2f})"

# ---------------------------------------------------------------------------
# 标识符格式（00-conventions §2）
# ---------------------------------------------------------------------------

TASK_ID_PATTERN = re.compile(r"^[a-z]+-[0-9]{3}$")
SEMVER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
RUBRIC_ITEM_ID_PATTERN = re.compile(r"^R[0-9]{1,3}$")
TRIAL_ID_PATTERN = re.compile(
    r"^(?P<task_id>[a-z]+-[0-9]{3})\."
    r"(?P<model>[a-z0-9]+(?:[.-][a-z0-9]+)*)\."
    r"(?P<shell>[a-z0-9]+(?:-[a-z0-9]+)*)\."
    r"e(?P<epoch>[0-9]+)\.t(?P<index>[0-9]{2})$"
)
BATCH_ID_PATTERN = re.compile(r"^(?P<year_month>\d{4}-(?:0[1-9]|1[0-2]))-b(?P<seq>\d+)$")
DEFECT_ID_PATTERN = re.compile(r"^(?P<rubric_item_id>R[0-9]{1,3})#(?P<round>[0-9]{1,2})$")


@dataclass(frozen=True)
class TrialId:
    """trial ID 的结构化形式：{task}.{model}.{shell}.e{epoch}.t{index}。"""

    task_id: str
    model: str
    shell: str
    epoch: int
    index: int

    @property
    def trial_id(self) -> str:
        """返回规范字符串形式。"""
        return format_trial_id(self)


def format_trial_id(parts: TrialId) -> str:
    """把 TrialId 渲染为规范 trial ID 字符串。"""
    return f"{parts.task_id}.{parts.model}.{parts.shell}.e{parts.epoch}.t{parts.index:02d}"


def parse_trial_id(trial_id: str) -> TrialId:
    """解析 trial ID；不合法时抛 ValueError（消息指出期望格式）。"""
    match = TRIAL_ID_PATTERN.match(trial_id)
    if match is None:
        raise ValueError(
            "trial ID 不合法，期望 {task_id}.{model}.{shell}.e{epoch}.t{index:02d}，"
            f"收到: {trial_id!r}"
        )
    return TrialId(
        task_id=match.group("task_id"),
        model=match.group("model"),
        shell=match.group("shell"),
        epoch=int(match.group("epoch")),
        index=int(match.group("index")),
    )


@dataclass(frozen=True)
class BatchId:
    """批次 ID：{YYYY-MM}-b{序号}。"""

    year_month: str
    seq: int

    @property
    def batch_id(self) -> str:
        """返回规范字符串形式。"""
        return f"{self.year_month}-b{self.seq}"


def parse_batch_id(batch_id: str) -> BatchId:
    """解析批次 ID；不合法时抛 ValueError。"""
    match = BATCH_ID_PATTERN.match(batch_id)
    if match is None:
        raise ValueError(
            f"批次 ID 不合法，期望 {{YYYY-MM}}-b{{序号}}，例如 2026-11-b1，收到: {batch_id!r}"
        )
    return BatchId(year_month=match.group("year_month"), seq=int(match.group("seq")))


def batch_id_is_valid(batch_id: str) -> bool:
    """批次 ID 是否合法（不抛异常的便捷判断）。"""
    return BATCH_ID_PATTERN.match(batch_id) is not None


def defect_id_for(rubric_item_id: str, round_no: int) -> str:
    """构造缺陷 ID：{rubric_item_id}#{轮次}（00-conventions §2）。"""
    if not RUBRIC_ITEM_ID_PATTERN.match(rubric_item_id):
        raise ValueError(f"rubric item ID 不合法: {rubric_item_id!r}")
    if round_no < 1 or round_no > 99:
        raise ValueError(f"轮次范围 1..99，收到: {round_no!r}")
    return f"{rubric_item_id}#{round_no}"


def parse_defect_id(defect_id: str) -> tuple[str, int]:
    """解析缺陷 ID，返回 (rubric_item_id, 轮次)。"""
    match = DEFECT_ID_PATTERN.match(defect_id)
    if match is None:
        raise ValueError(f"缺陷 ID 不合法，期望 R1#2 形式，收到: {defect_id!r}")
    return match.group("rubric_item_id"), int(match.group("round"))


# ---------------------------------------------------------------------------
# 档位辅助函数
# ---------------------------------------------------------------------------


def tier_value(tier: Tier) -> int:
    """档位 -> 数值（P=7 ... F=0）。"""
    return TIER_VALUE[tier]


def display_tier(tier: Tier) -> str:
    """档位 -> 榜单展示文案。"""
    return TIER_DISPLAY[tier]


def tier_by_value(value: int) -> Tier:
    """数值 -> 档位；超出 0..7 抛 ValueError。"""
    for tier, mapped in TIER_VALUE.items():
        if mapped == value:
            return tier
    raise ValueError(f"档位数值超出 0..7: {value!r}")


def median_tier(tiers: list[Tier] | tuple[Tier, ...]) -> Tier:
    """求中位档位（00-conventions §4.3 数值映射）。

    奇数个样本取正中间；偶数个样本取较低的一侧（较差档位），偏保守。
    """
    if not tiers:
        raise ValueError("无法对空档位序列求中位数")
    values = sorted(TIER_VALUE[tier] for tier in tiers)
    return tier_by_value(values[(len(values) - 1) // 2])


def max_feedback_level(
    levels: list[FeedbackLevel] | tuple[FeedbackLevel, ...],
) -> FeedbackLevel | None:
    """返回用过的最高反馈级别；空序列返回 None。"""
    if not levels:
        return None
    return max(levels, key=lambda level: FEEDBACK_LEVEL_ORDER.index(level))


# ---------------------------------------------------------------------------
# 预算（00-conventions §4.8 字段名冻结）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationPolicy:
    """反馈升级阈值（00-conventions §4.8）。"""

    f1_no_progress_rounds: int = 2
    f2_no_progress_rounds: int = 2

    def __post_init__(self) -> None:
        if self.f1_no_progress_rounds < 1 or self.f2_no_progress_rounds < 1:
            raise ValueError("升级阈值至少为 1 轮")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EscalationPolicy:
        """从 task.yaml/escalation 对象构造；None 返回默认值。"""
        data = data or {}
        return cls(
            f1_no_progress_rounds=int(data.get("f1_no_progress_rounds", 2)),
            f2_no_progress_rounds=int(data.get("f2_no_progress_rounds", 2)),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "f1_no_progress_rounds": self.f1_no_progress_rounds,
            "f2_no_progress_rounds": self.f2_no_progress_rounds,
        }


@dataclass(frozen=True)
class Budget:
    """单个 trial 的预算上限（00-conventions §4.8，标准题默认值）。"""

    max_rounds: int = 6
    max_feedback_rounds: int = 3
    max_nudges: int = 2
    max_total_tokens: int = 2_000_000
    max_wall_clock_s: int = 3600
    max_cost_usd: float = 15.0
    max_observe_s: int = 600

    @classmethod
    def heavy(cls) -> Budget:
        """重型题默认预算（docs/02 §7.1 右列）。"""
        return cls(
            max_rounds=10,
            max_feedback_rounds=5,
            max_nudges=2,
            max_total_tokens=8_000_000,
            max_wall_clock_s=10800,
            max_cost_usd=80.0,
            max_observe_s=1200,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        """从 task.yaml 的 budget 对象构造（缺省字段用标准题默认值）。"""
        defaults = cls()
        return cls(
            max_rounds=int(data.get("max_rounds", defaults.max_rounds)),
            max_feedback_rounds=int(data.get("max_feedback_rounds", defaults.max_feedback_rounds)),
            max_nudges=int(data.get("max_nudges", defaults.max_nudges)),
            max_total_tokens=int(data.get("max_total_tokens", defaults.max_total_tokens)),
            max_wall_clock_s=int(data.get("max_wall_clock_s", defaults.max_wall_clock_s)),
            max_cost_usd=float(data.get("max_cost_usd", defaults.max_cost_usd)),
            max_observe_s=int(data.get("max_observe_s", defaults.max_observe_s)),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "max_rounds": self.max_rounds,
            "max_feedback_rounds": self.max_feedback_rounds,
            "max_nudges": self.max_nudges,
            "max_total_tokens": self.max_total_tokens,
            "max_wall_clock_s": self.max_wall_clock_s,
            "max_cost_usd": self.max_cost_usd,
            "max_observe_s": self.max_observe_s,
        }


@dataclass
class BudgetUsage:
    """预算实际消耗（可变，由 runner 更新；docs/02 §7.2 强制点）。"""

    rounds: int = 0
    feedback_rounds: int = 0
    nudges: int = 0
    total_tokens: int = 0
    wall_clock_s: float = 0.0
    agent_cost_usd: float = 0.0
    judge_cost_usd: float = 0.0
    user_cost_usd: float = 0.0

    @property
    def cost(self) -> Cost:
        """费用拆分视图（grade.cost 口径）。"""
        return Cost(
            agent=self.agent_cost_usd,
            judge=self.judge_cost_usd,
            user=self.user_cost_usd,
        )

    def add_round(
        self,
        *,
        tokens: int = 0,
        agent_cost_usd: float = 0.0,
        judge_cost_usd: float = 0.0,
        user_cost_usd: float = 0.0,
        wall_clock_s: float = 0.0,
    ) -> BudgetUsage:
        """记录一轮 Agent 运行消耗，返回 self 以便链式调用。"""
        if tokens < 0 or wall_clock_s < 0:
            raise ValueError("消耗不能为负")
        self.rounds += 1
        self.total_tokens += tokens
        self.agent_cost_usd += agent_cost_usd
        self.judge_cost_usd += judge_cost_usd
        self.user_cost_usd += user_cost_usd
        self.wall_clock_s += wall_clock_s
        return self

    def add_feedback_round(self, *, user_cost_usd: float = 0.0) -> BudgetUsage:
        """记录一次真实反馈（F1-F3；F0 催促不计入）。"""
        self.feedback_rounds += 1
        self.user_cost_usd += user_cost_usd
        return self

    def add_nudge(self, *, user_cost_usd: float = 0.0) -> BudgetUsage:
        """记录一次 F0 无信息量催促。"""
        self.nudges += 1
        self.user_cost_usd += user_cost_usd
        return self

    def observe_timeout(self, budget: Budget, *, observe_s: float) -> bool:
        """单轮观察是否超过 max_observe_s。"""
        return observe_s > budget.max_observe_s

    def exhausted_reasons(self, budget: Budget) -> tuple[str, ...]:
        """返回已经触底的预算项（空元组表示预算仍可用）。"""
        reasons: list[str] = []
        if self.rounds >= budget.max_rounds:
            reasons.append(f"rounds={self.rounds}/{budget.max_rounds}")
        if self.feedback_rounds >= budget.max_feedback_rounds:
            reasons.append(f"feedback_rounds={self.feedback_rounds}/{budget.max_feedback_rounds}")
        if self.total_tokens >= budget.max_total_tokens:
            reasons.append(f"total_tokens={self.total_tokens}/{budget.max_total_tokens}")
        if self.wall_clock_s >= budget.max_wall_clock_s:
            reasons.append(f"wall_clock_s={self.wall_clock_s:.0f}/{budget.max_wall_clock_s}")
        if self.agent_cost_usd >= budget.max_cost_usd:
            reasons.append(f"agent_cost_usd={self.agent_cost_usd:.2f}/{budget.max_cost_usd:.2f}")
        return tuple(reasons)

    def exhausted_by(self, budget: Budget) -> str | None:
        """返回首个触底的预算项名称（rounds/tokens/wall_clock/cost），未触底为 None。"""
        checks = (
            ("rounds", self.rounds >= budget.max_rounds),
            ("tokens", self.total_tokens >= budget.max_total_tokens),
            ("wall_clock", self.wall_clock_s >= budget.max_wall_clock_s),
            ("cost", self.agent_cost_usd >= budget.max_cost_usd),
        )
        for name, hit in checks:
            if hit:
                return name
        return None

    def can_continue(self, budget: Budget) -> bool:
        """是否还能再跑一轮。"""
        return not self.exhausted_reasons(budget)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict；并附带 feedback.schema.json budget_snapshot 所需字段。"""
        return {
            "rounds": self.rounds,
            "feedback_rounds": self.feedback_rounds,
            "nudges": self.nudges,
            "total_tokens": self.total_tokens,
            "wall_clock_s": round(self.wall_clock_s, 3),
            "agent_cost_usd": round(self.agent_cost_usd, 6),
            "judge_cost_usd": round(self.judge_cost_usd, 6),
            "user_cost_usd": round(self.user_cost_usd, 6),
        }


# ---------------------------------------------------------------------------
# 费用（grade.schema.json $defs.cost）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cost:
    """费用拆分（docs/02 §7.3；grade.cost 契约）。"""

    agent: float = 0.0
    judge: float = 0.0
    user: float = 0.0
    currency: str = "USD"
    pricing_table_version: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("agent", self.agent), ("judge", self.judge), ("user", self.user)):
            if value < 0:
                raise ValueError(f"cost.{name} 不能为负: {value!r}")
        if self.currency != "USD":
            raise ValueError(f"计价货币固定 USD，收到: {self.currency!r}")

    @property
    def total(self) -> float:
        """三项之和（grade.cost.total）。"""
        return round(self.agent + self.judge + self.user, 6)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "agent": round(self.agent, 6),
            "judge": round(self.judge, 6),
            "user": round(self.user, 6),
            "total": self.total,
            "currency": self.currency,
        }
        if self.pricing_table_version:
            payload["pricing_table_version"] = self.pricing_table_version
        return payload


# ---------------------------------------------------------------------------
# rubric（schemas/rubric.schema.json）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricItem:
    """一条原子化 rubric 条目（rubric.schema.json $defs.rubric_item）。

    weight 不在 schema 中，是由 severity 派生的只读属性（00-conventions §4.4）。
    """

    id: str
    severity: Severity = Severity.MINOR
    description: str = ""
    f1_symptom: str = ""
    kind: CheckKind = CheckKind.LLM_JUDGE
    judge_question: str = ""
    pass_criteria: str = ""
    fail_examples: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    evidence_required: tuple[str, ...] = ()
    f2_evidence: str = ""
    f3_visual: str = ""
    f3_assist: str = ""
    hidden: bool = False
    tags: tuple[str, ...] = ()
    author: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not RUBRIC_ITEM_ID_PATTERN.match(self.id):
            raise ValueError(f"rubric item ID 不合法（R1..R999）: {self.id!r}")
        object.__setattr__(self, "fail_examples", tuple(self.fail_examples))
        object.__setattr__(self, "applies_to", tuple(self.applies_to))
        object.__setattr__(self, "evidence_required", tuple(self.evidence_required))
        object.__setattr__(self, "tags", tuple(self.tags))

    @property
    def weight(self) -> int:
        """扣分权重：minor=1 / functional=3 / fatal=5（由 severity 派生）。"""
        return PENALTY_WEIGHT[self.severity]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 rubric.schema.json 的 item 对象（可选字段为空时省略）。"""
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "severity": str(self.severity),
            "description": self.description,
            "f1_symptom": self.f1_symptom,
        }
        optional: tuple[tuple[str, Any], ...] = (
            ("judge_question", self.judge_question),
            ("pass_criteria", self.pass_criteria),
            ("fail_examples", list(self.fail_examples)),
            ("applies_to", list(self.applies_to)),
            ("evidence_required", list(self.evidence_required)),
            ("f2_evidence", self.f2_evidence),
            ("f3_visual", self.f3_visual),
            ("f3_assist", self.f3_assist),
            ("tags", list(self.tags)),
            ("author", self.author),
            ("notes", self.notes),
        )
        for key, value in optional:
            if value:
                payload[key] = value
        if self.hidden:
            payload["hidden"] = True
        return payload


@dataclass
class RubricSet:
    """一个题目的 rubric 集合（rubric.schema.json 顶层文档）。"""

    task_id: str
    items: tuple[RubricItem, ...]
    rubric_version: str = "1.0.0"
    title: str = ""
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if not TASK_ID_PATTERN.match(self.task_id):
            raise ValueError(f"task_id 不合法: {self.task_id!r}")
        if not SEMVER_PATTERN.match(self.rubric_version):
            raise ValueError(f"rubric_version 不是 semver: {self.rubric_version!r}")
        self.items = tuple(self.items)
        if not self.items:
            raise ValueError("rubric 集合不能为空")
        if len(self.items) > 40:
            raise ValueError("rubric 条目最多 40 条（rubric.schema.json）")
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric item ID 重复")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RubricSet:
        """从 rubric 文档 JSON 构造。"""
        return cls(
            task_id=str(data["task_id"]),
            rubric_version=str(data.get("rubric_version", "1.0.0")),
            title=str(data.get("title", "")),
            schema_version=str(data.get("schema_version", "1.0")),
            items=tuple(_rubric_item_from_dict(item) for item in data.get("items", ())),
        )

    def to_document(self) -> dict[str, Any]:
        """序列化为 rubric 文档（可直接 json.dumps 并用 schema 校验）。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "rubric_version": self.rubric_version,
            "items": [item.to_dict() for item in self.items],
        }
        if self.title:
            payload["title"] = self.title
        return payload

    def get(self, item_id: str) -> RubricItem:
        """按 ID 取条目；不存在抛 KeyError。"""
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def blocking_items(self) -> tuple[RubricItem, ...]:
        """severity ∈ {functional, fatal} 的条目（阻断验收）。"""
        return tuple(
            item for item in self.items if item.severity in (Severity.FUNCTIONAL, Severity.FATAL)
        )


def _rubric_item_from_dict(item: dict[str, Any]) -> RubricItem:
    """从 JSON 构造单条 rubric 条目（内部辅助）。"""
    return RubricItem(
        id=str(item["id"]),
        severity=Severity(str(item.get("severity", "minor"))),
        description=str(item.get("description", "")),
        f1_symptom=str(item.get("f1_symptom", "")),
        kind=CheckKind(str(item.get("kind", "llm_judge"))),
        judge_question=str(item.get("judge_question", "")),
        pass_criteria=str(item.get("pass_criteria", "")),
        fail_examples=tuple(item.get("fail_examples", ())),
        applies_to=tuple(item.get("applies_to", ())),
        evidence_required=tuple(item.get("evidence_required", ())),
        f2_evidence=str(item.get("f2_evidence", "")),
        f3_visual=str(item.get("f3_visual", "")),
        f3_assist=str(item.get("f3_assist", "")),
        hidden=bool(item.get("hidden", False)),
        tags=tuple(item.get("tags", ())),
        author=str(item.get("author", "")),
        notes=str(item.get("notes", "")),
    )


# ---------------------------------------------------------------------------
# 缺陷（schemas/defect.schema.json $defs.observation_item）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """一条可复核证据（defect.schema.json $defs.evidence）。"""

    kind: EvidenceKind
    uri: str
    sha256: str | None = None
    excerpt: str | None = None
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {"kind": str(self.kind), "uri": self.uri}
        if self.sha256:
            payload["sha256"] = self.sha256
        if self.excerpt:
            payload["excerpt"] = self.excerpt
        if self.caption:
            payload["caption"] = self.caption
        return payload

    @classmethod
    def from_any(cls, raw: str | Evidence | dict[str, Any]) -> Evidence:
        """把字符串/字典/Evidence 统一成 Evidence（字符串按 other/uri 处理）。"""
        if isinstance(raw, Evidence):
            return raw
        if isinstance(raw, str):
            return cls(kind=EvidenceKind.OTHER, uri=raw)
        return cls(
            kind=EvidenceKind(str(raw.get("kind", "other"))),
            uri=str(raw["uri"]),
            sha256=raw.get("sha256"),
            excerpt=raw.get("excerpt"),
            caption=raw.get("caption"),
        )


@dataclass
class Defect:
    """一个已登记的缺陷（defect.schema.json observation_item 的运行时镜像）。

    ID 规则：{rubric_item_id}#{首次发现轮次}（00-conventions §2）。
    同一 rubric 条目同一时刻最多一个未修复缺陷；修复后复发按新缺陷重新计扣分。
    """

    defect_id: str
    rubric_item_id: str
    severity: Severity
    status: DefectStatus
    first_round: int
    last_round: int
    rounds_failed: tuple[int, ...] = ()
    fixed_round: int | None = None
    feedback_rounds: int = 0
    max_level_during_open: FeedbackLevel | None = None
    progress: DefectProgress = DefectProgress.NONE
    evidence: tuple[Evidence, ...] = ()
    judge: dict[str, Any] | None = None
    note: str = ""

    def __post_init__(self) -> None:
        item_id, round_no = parse_defect_id(self.defect_id)
        if item_id != self.rubric_item_id:
            raise ValueError(
                f"缺陷 ID {self.defect_id!r} 与 rubric_item_id {self.rubric_item_id!r} 不一致"
            )
        if round_no != self.first_round:
            raise ValueError(
                f"缺陷 ID {self.defect_id!r} 的轮次 {round_no} 与 first_round {self.first_round} 不一致"
            )
        if self.first_round < 1 or self.last_round < self.first_round:
            raise ValueError("轮次不合法：要求 1 <= first_round <= last_round")
        if not self.rounds_failed:
            self.rounds_failed = (self.first_round,)
        rounds = tuple(sorted(set(self.rounds_failed)))
        self.rounds_failed = rounds
        self.evidence = tuple(Evidence.from_any(item) for item in self.evidence)

    @property
    def id(self) -> str:
        """兼容别名：defect_id。"""
        return self.defect_id

    @property
    def penalty_weight(self) -> int:
        """按严重度返回扣分权重（minor=1 / functional=3 / fatal=5）。"""
        return PENALTY_WEIGHT[self.severity]

    @property
    def is_open(self) -> bool:
        """是否仍未修复（new/open/persisting）。"""
        return self.status in (DefectStatus.NEW, DefectStatus.OPEN, DefectStatus.PERSISTING)

    @property
    def is_blocking(self) -> bool:
        """是否阻断验收（未修复且 severity ∈ {functional, fatal}）。"""
        return self.is_open and self.severity in (Severity.FUNCTIONAL, Severity.FATAL)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 defect.schema.json 的 observation_item。"""
        payload: dict[str, Any] = {
            "defect_id": self.defect_id,
            "rubric_item_id": self.rubric_item_id,
            "severity": str(self.severity),
            "status": str(self.status),
            "first_round": self.first_round,
            "last_round": self.last_round,
            "rounds_failed": list(self.rounds_failed),
            "fixed_round": self.fixed_round,
            "feedback_rounds": self.feedback_rounds,
            "max_level_during_open": (
                None if self.max_level_during_open is None else str(self.max_level_during_open)
            ),
            "progress": str(self.progress),
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.judge is not None:
            payload["judge"] = dict(self.judge)
        if self.note:
            payload["note"] = self.note
        return payload

    def to_summary_item(self, *, counted: bool = True) -> dict[str, Any]:
        """序列化为 grade.schema.json 的 defect_summary_item。"""
        payload: dict[str, Any] = {
            "defect_id": self.defect_id,
            "rubric_item_id": self.rubric_item_id,
            "severity": str(self.severity),
            "status": str(self.status),
            "first_round": self.first_round,
            "feedback_rounds": self.feedback_rounds,
            "counted": counted,
        }
        if self.fixed_round is not None:
            payload["fixed_round"] = self.fixed_round
        payload["max_level_during_open"] = (
            None if self.max_level_during_open is None else str(self.max_level_during_open)
        )
        return payload


# ---------------------------------------------------------------------------
# grade 子结构（schemas/grade.schema.json $defs）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardGateSummary:
    """L1 硬门槛结果摘要。"""

    passed: bool = False
    total: int = 0
    failed: tuple[str, ...] = ()
    evidence_uri: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "failed", tuple(self.failed))

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "passed": self.passed,
            "total": self.total,
            "failed": list(self.failed),
        }
        if self.evidence_uri is not None:
            payload["evidence_uri"] = self.evidence_uri
        return payload


@dataclass(frozen=True)
class DefectSummary:
    """判分时的缺陷汇总（grade.schema.json $defs.defect_summary）。"""

    total: int = 0
    by_severity: dict[str, int] = field(
        default_factory=lambda: {"minor": 0, "functional": 0, "fatal": 0}
    )
    open_blocking: int = 0
    fixed: int = 0
    requiring_feedback: int = 0
    multi_round_f1: int = 0
    regressed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "total": self.total,
            "by_severity": dict(self.by_severity),
            "open_blocking": self.open_blocking,
            "fixed": self.fixed,
            "requiring_feedback": self.requiring_feedback,
            "multi_round_f1": self.multi_round_f1,
            "regressed": self.regressed,
        }


@dataclass(frozen=True)
class FeedbackSummary:
    """反馈阶梯使用汇总（grade.schema.json $defs.feedback_summary）。"""

    max_level: FeedbackLevel = FeedbackLevel.F0
    feedback_rounds: int = 0
    level_counts: dict[str, int] = field(
        default_factory=lambda: {"F0": 0, "F1": 0, "F2": 0, "F3": 0}
    )
    nudges_used: int = 0
    total_defects: int = 0
    need_feedback: int = 0
    half_step_applied: bool = False
    multi_round_f1: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "max_level": str(self.max_level),
            "feedback_rounds": self.feedback_rounds,
            "level_counts": dict(self.level_counts),
            "nudges_used": self.nudges_used,
            "total_defects": self.total_defects,
            "need_feedback": self.need_feedback,
            "half_step_applied": self.half_step_applied,
            "multi_round_f1": self.multi_round_f1,
        }


@dataclass(frozen=True)
class PenaltyItem:
    """单条扣分明细（grade.schema.json $defs.penalty_item）。"""

    defect_id: str
    rubric_item_id: str
    severity: Severity
    weight: int
    first_round: int
    counted: bool = True

    def __post_init__(self) -> None:
        expected = PENALTY_WEIGHT[self.severity]
        if self.weight != expected:
            raise ValueError(f"{self.severity} 的权重必须是 {expected}，收到 {self.weight}")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "defect_id": self.defect_id,
            "rubric_item_id": self.rubric_item_id,
            "severity": str(self.severity),
            "weight": self.weight,
            "first_round": self.first_round,
            "counted": self.counted,
        }


@dataclass(frozen=True)
class Consistency:
    """裁判一致性指标（grade.schema.json $defs.consistency）。"""

    metric: str = "percent_agreement"
    value: float | None = None
    samples: int = 0
    threshold: float | None = None
    note: str | None = None

    _METRICS = ("cohen_kappa", "krippendorff_alpha", "percent_agreement")

    def __post_init__(self) -> None:
        if self.metric not in self._METRICS:
            raise ValueError(f"metric 必须是 {self._METRICS} 之一，收到 {self.metric!r}")
        if self.value is not None and not -1.0 <= self.value <= 1.0:
            raise ValueError("consistency.value 必须在 [-1, 1]")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "metric": self.metric,
            "value": self.value,
            "samples": self.samples,
        }
        if self.threshold is not None:
            payload["threshold"] = self.threshold
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class Decision:
    """判定规则命中记录（grade.schema.json $defs.decision）。"""

    rule_id: RuleId
    priority: int
    rationale: str

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 99:
            raise ValueError(f"priority 必须在 0..99，收到 {self.priority!r}")
        if len(self.rationale) < 8:
            raise ValueError("rationale 至少 8 个字符（说明命中理由与关键输入量）")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "rule_id": str(self.rule_id),
            "priority": self.priority,
            "rationale": self.rationale,
        }

    @classmethod
    def for_rule(cls, rule_id: RuleId, rationale: str) -> Decision:
        """按规则 ID 自动填充优先级。"""
        return cls(
            rule_id=rule_id,
            priority=RULE_PRIORITIES[rule_id],
            rationale=rationale,
        )


@dataclass(frozen=True)
class HumanOverride:
    """人工覆盖记录（仅 decided_by ∈ {human, arbiter} 时出现）。"""

    reviewer: str
    reason: str
    at: str
    previous_tier: Tier | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "reviewer": self.reviewer,
            "reason": self.reason,
            "at": self.at,
        }
        payload["previous_tier"] = None if self.previous_tier is None else str(self.previous_tier)
        return payload


@dataclass(frozen=True)
class JudgeMember:
    """评审团成员（grade.schema.json $defs.judge_member）。"""

    model: str
    provider: str
    family: str
    role: str = "llm_judge"
    samples: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "provider": self.provider,
            "family": self.family,
            "role": self.role,
        }
        if self.samples is not None:
            payload["samples"] = self.samples
        return payload


@dataclass
class Grade:
    """一个 grade 版本（schemas/grade.schema.json 的运行时镜像）。"""

    trial_id: str
    tier: Tier
    decision: Decision
    decided_at: str
    hard_gate_summary: HardGateSummary = field(default_factory=HardGateSummary)
    defect_summary: DefectSummary = field(default_factory=DefectSummary)
    feedback_summary: FeedbackSummary = field(default_factory=FeedbackSummary)
    cost: Cost = field(default_factory=Cost)
    consistency: Consistency = field(default_factory=Consistency)
    penalty: int = 0
    grade_version: int = 1
    supersedes: int | None = None
    status: GradeStatus = GradeStatus.FINAL
    validity: Validity = Validity.VALID
    flags: tuple[GradeFlag, ...] = ()
    decided_by: DecidedBy = DecidedBy.AUTO
    penalty_breakdown: tuple[PenaltyItem, ...] = ()
    manual_notes: str | None = None
    human_override: HumanOverride | None = None
    rubric_version: str = "1.0.0"
    task_version: str = "1.0.0"
    judge_panel: tuple[JudgeMember, ...] = ()
    defects: tuple[dict[str, Any], ...] = ()
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.grade_version < 1:
            raise ValueError(f"grade 版本从 1 开始，收到: {self.grade_version!r}")
        if self.supersedes is not None and self.supersedes >= self.grade_version:
            raise ValueError(
                f"supersedes({self.supersedes}) 必须小于当前版本({self.grade_version})"
            )
        if self.penalty < 0:
            raise ValueError("penalty 不能为负")
        self.flags = tuple(GradeFlag(str(flag)) for flag in self.flags)
        self.penalty_breakdown = tuple(self.penalty_breakdown)
        self.judge_panel = tuple(self.judge_panel)
        self.defects = tuple(self.defects)
        if self.decided_by in (DecidedBy.HUMAN, DecidedBy.ARBITER):
            if not self.manual_notes:
                raise ValueError("decided_by=human/arbiter 时必须填写 manual_notes")
            if self.human_override is None:
                raise ValueError("decided_by=human/arbiter 时必须填写 human_override")
        if self.status == GradeStatus.SUPERSEDED and self.supersedes is not None:
            raise ValueError("已被替代的版本不应再有 supersedes 指向")

    @property
    def schema_version(self) -> str:
        """本模块实现的 grade schema 版本。"""
        return "1.0"

    @property
    def tier_numeric(self) -> int:
        """档位数值映射（P=7 ... F=0）。"""
        return TIER_VALUE[self.tier]

    @property
    def counted_in_leaderboard(self) -> bool:
        """是否计入榜单：valid 且 final 才为 true；作弊（valid+final）仍计入（记 0）。"""
        return self.validity == Validity.VALID and self.status == GradeStatus.FINAL

    def to_dict(self) -> dict[str, Any]:
        """序列化为 grade.schema.json 对象。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "trial_id": self.trial_id,
            "grade_version": self.grade_version,
            "supersedes": self.supersedes,
            "tier": str(self.tier),
            "tier_numeric": self.tier_numeric,
            "status": str(self.status),
            "validity": str(self.validity),
            "counted_in_leaderboard": self.counted_in_leaderboard,
            "flags": [str(flag) for flag in self.flags],
            "decided_by": str(self.decided_by),
            "decided_at": self.decided_at,
            "hard_gate_summary": self.hard_gate_summary.to_dict(),
            "defect_summary": self.defect_summary.to_dict(),
            "feedback_summary": self.feedback_summary.to_dict(),
            "penalty": self.penalty,
            "penalty_breakdown": [item.to_dict() for item in self.penalty_breakdown],
            "cost": self.cost.to_dict(),
            "consistency": self.consistency.to_dict(),
            "decision": self.decision.to_dict(),
            "rubric_version": self.rubric_version,
            "task_version": self.task_version,
            "judge_panel": [member.to_dict() for member in self.judge_panel],
        }
        if self.manual_notes is not None:
            payload["manual_notes"] = self.manual_notes
        if self.human_override is not None:
            payload["human_override"] = self.human_override.to_dict()
        if self.defects:
            payload["defects"] = list(self.defects)
        if self.extensions:
            payload["extensions"] = dict(self.extensions)
        return payload


# ---------------------------------------------------------------------------
# trial 记录（完整 trial schema 由 isolation 队友维护，字段落地后校准）
# ---------------------------------------------------------------------------


@dataclass
class TrialError:
    """trial 级错误：类别 + 处置（00-conventions §4.6）。"""

    error_class: ErrorClass
    cause: str = ""
    message: str = ""
    disposition: Disposition | None = None

    def __post_init__(self) -> None:
        expected = DEFAULT_DISPOSITIONS[self.error_class]
        if self.disposition is None:
            self.disposition = expected
        elif self.disposition != expected:
            raise ValueError(
                f"错误类别 {self.error_class} 的默认处置是 {expected}，"
                f"不能强制改为 {self.disposition}（如确需覆盖请改 DEFAULT_DISPOSITIONS）"
            )

    @property
    def validity(self) -> Validity:
        """该错误对成绩有效性的影响。"""
        assert self.disposition is not None
        return DISPOSITION_VALIDITY[self.disposition]

    @property
    def retryable(self) -> bool:
        """是否应重试。"""
        return self.disposition in (Disposition.RETRY, Disposition.DISCARD)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "error_class": str(self.error_class),
            "cause": self.cause,
            "message": self.message,
            "disposition": str(self.disposition),
            "validity": str(self.validity),
            "retryable": self.retryable,
        }


@dataclass
class TrialRecord:
    """一个 trial 的持久化记录（schemas/trial.schema.json 的运行时镜像）。

    state 由 evalspleef.core.state.TrialMachine 维护；完整 trial schema 由 isolation
    维护，字段落地后逐项校准 to_dict()。
    """

    trial_id: str
    state: TrialState = TrialState.PENDING
    validity: Validity = Validity.PENDING
    grades: list[Grade] = field(default_factory=list)
    errors: list[TrialError] = field(default_factory=list)
    canary_token: str = ""
    task_id: str = ""
    model: str = ""
    shell: str = ""
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trial_id:
            raise ValueError("trial_id 不能为空")
        try:
            parts = parse_trial_id(self.trial_id)
        except ValueError:
            return
        self.task_id = self.task_id or parts.task_id
        self.model = self.model or parts.model
        self.shell = self.shell or parts.shell
        self.extensions.setdefault("epoch", parts.epoch)
        self.extensions.setdefault("index", parts.index)

    def current_grade(self) -> Grade | None:
        """当前生效的 grade（最新版本）；未判分时返回 None。"""
        return self.grades[-1] if self.grades else None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "trial_id": self.trial_id,
            "task_id": self.task_id,
            "model": self.model,
            "shell": self.shell,
            "state": str(self.state),
            "validity": str(self.validity),
            "canary_token": self.canary_token,
            "grades": [grade.to_dict() for grade in self.grades],
            "errors": [error.to_dict() for error in self.errors],
            "extensions": dict(self.extensions),
        }


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def to_plain(value: Any) -> Any:
    """把枚举 / dataclass / 容器递归转换为可 json.dumps 的普通对象。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_plain(to_dict())
    raise TypeError(f"无法序列化的对象: {type(value).__name__}")
