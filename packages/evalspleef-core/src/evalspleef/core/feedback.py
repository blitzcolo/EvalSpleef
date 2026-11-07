"""evalspleef.core.feedback — 反馈阶梯（F0-F3）控制器与泄露检查。

规格依据：drafts/plan-v1.md §4.3（反馈阶梯表、控制器流程）与
docs/00-conventions.md §4.2（级别拼写）。docs/03-grading.md 出现后以它为准。

控制器原则
----------
1. 先用最低级别：新缺陷一律从 F1 开始；
2. 同一缺陷 F1 连续 2 轮无进展 -> F2；F2 再连续 2 轮无进展 -> F3
   （阈值来自 00-conventions §4.8 escalation，默认各 2 轮）；
   到 F3 后维持 F3 直到修复或预算耗尽；
3. 每条反馈只讲一个缺陷（FeedbackRound.defect_ids 最多 1 个）；
4. 每级有字段白名单：模拟用户只能拿到该级允许的字段；
5. 文本生成后过泄露检查（正则集 + 可插拔模型检查器接口），命中即要求重写；
6. 轮数/费用/墙钟预算耗尽即停止反馈。

字段白名单（可被 docs/03 覆盖，当前按 plan-v1 §4.3 表格落地）
------------------------------------------------------------
F0: 无（只能说我试用了一下 / 直接结束）
F1: symptom（普通用户视角的现象描述）
F2: symptom + log_quote + error_text（日志、报错原文）
F3: symptom + visual_diff + assist_operation（视觉描述、协助操作）
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .defects import DefectTracker
from .models import (
    Budget,
    BudgetUsage,
    Defect,
    EscalationPolicy,
    FeedbackLevel,
    Severity,
)

__all__ = [
    "ALLOWED_FIELDS",
    "FORBIDDEN_BY_LEVEL",
    "LEAK_RULE_IDS",
    "CallableLeakChecker",
    "CompositeLeakChecker",
    "F0Policy",
    "FeedbackDecision",
    "FeedbackLadder",
    "FeedbackRecord",
    "FeedbackRound",
    "FeedbackValidation",
    "LeakCategory",
    "LeakChecker",
    "LeakViolation",
    "RegexLeakChecker",
    "allowed_fields",
    "budget_snapshot",
    "build_feedback_record",
    "build_leak_check",
    "consume_decision",
    "filter_allowed_fields",
    "validate_feedback_message",
]


# ---------------------------------------------------------------------------
# 字段白名单
# ---------------------------------------------------------------------------

#: 各反馈级别允许暴露给模拟用户的字段（自低向高累计）。
#: 键名与 feedback.schema.json $defs.allowed_field 逐字一致。
ALLOWED_FIELDS: dict[FeedbackLevel, frozenset[str]] = {
    FeedbackLevel.F0: frozenset(),
    FeedbackLevel.F1: frozenset({"f1_symptom"}),
    FeedbackLevel.F2: frozenset({"f1_symptom", "f2_evidence"}),
    FeedbackLevel.F3: frozenset({"f1_symptom", "f2_evidence", "f3_visual", "f3_assist"}),
}


def allowed_fields(level: FeedbackLevel, *, hidden: bool = False) -> frozenset[str]:
    """返回某级别允许的字段集合。

    hidden=True 表示条目对应隐藏验收：任何级别都只能用 f1_symptom
    （docs/03 §7.3 白名单矩阵：hidden 条目 F2/F3 素材一律禁止）。
    """
    if hidden:
        return frozenset() if level == FeedbackLevel.F0 else frozenset({"f1_symptom"})
    return ALLOWED_FIELDS[level]


def filter_allowed_fields(
    fields: Mapping[str, str],
    level: FeedbackLevel,
    *,
    hidden: bool = False,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """按级别白名单过滤待生成字段，返回 (保留字段, 被丢弃字段名)。

    模拟用户生成反馈前必须调用本函数，避免控制器误把日志原文塞进 F1。
    hidden=True 时只保留 f1_symptom（docs/03 §7.3）。
    """
    permitted = allowed_fields(level, hidden=hidden)
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in fields.items():
        if key in permitted:
            kept[key] = value
        else:
            dropped.append(key)
    return kept, tuple(sorted(dropped))


# ---------------------------------------------------------------------------
# 泄露检查
# ---------------------------------------------------------------------------


class LeakCategory(str, Enum):
    """泄露类别，键名与 feedback.schema.json $defs.leak_category 逐字一致。"""

    FILE_NAME = "file_name"
    FUNCTION_NAME = "function_name"
    API_NAME = "api_name"
    LOG_TEXT = "log_text"
    FIX_SUGGESTION = "fix_suggestion"
    CODE = "code"
    ANSWER = "answer"
    HIDDEN_TEST = "hidden_test"
    INTERNAL_TERM = "internal_term"
    CANARY = "canary"
    PROMPT_INJECTION = "prompt_injection"
    OTHER = "other"
    MULTIPLE_DEFECTS = "multiple_defects"


#: 泄露类别 -> 正则规则 ID（LR-01..LR-14 由 appendix/personas/leak-rules.md 定义；
#: 本表是参考实现的临时映射，规则文档落地后逐条校准）。
LEAK_RULE_IDS: dict[LeakCategory, str] = {
    LeakCategory.FILE_NAME: "LR-01",
    LeakCategory.FUNCTION_NAME: "LR-02",
    LeakCategory.API_NAME: "LR-03",
    LeakCategory.LOG_TEXT: "LR-04",
    LeakCategory.FIX_SUGGESTION: "LR-05",
    LeakCategory.CODE: "LR-06",
    LeakCategory.ANSWER: "LR-07",
    LeakCategory.HIDDEN_TEST: "LR-08",
    LeakCategory.INTERNAL_TERM: "LR-09",
    LeakCategory.CANARY: "LR-10",
    LeakCategory.PROMPT_INJECTION: "LR-11",
    LeakCategory.MULTIPLE_DEFECTS: "LR-12",
    LeakCategory.OTHER: "LR-13",
}


#: 各级别禁止出现的类别；F0 连"具体问题"都不允许，因此禁止全部类别。
#: answer/hidden_test/internal_term/canary/prompt_injection 在任何级别都禁止。
_FORBIDDEN_ALWAYS: frozenset[LeakCategory] = frozenset(
    {
        LeakCategory.ANSWER,
        LeakCategory.HIDDEN_TEST,
        LeakCategory.INTERNAL_TERM,
        LeakCategory.CANARY,
        LeakCategory.PROMPT_INJECTION,
    }
)
FORBIDDEN_BY_LEVEL: dict[FeedbackLevel, frozenset[LeakCategory]] = {
    FeedbackLevel.F0: frozenset(LeakCategory),
    FeedbackLevel.F1: frozenset(
        {
            LeakCategory.FILE_NAME,
            LeakCategory.FUNCTION_NAME,
            LeakCategory.API_NAME,
            LeakCategory.LOG_TEXT,
            LeakCategory.FIX_SUGGESTION,
            LeakCategory.CODE,
        }
    )
    | _FORBIDDEN_ALWAYS,
    FeedbackLevel.F2: frozenset({LeakCategory.FIX_SUGGESTION, LeakCategory.CODE})
    | _FORBIDDEN_ALWAYS,
    FeedbackLevel.F3: frozenset({LeakCategory.CODE}) | _FORBIDDEN_ALWAYS,
}

#: 三个反引号的 fenced code block 检测（chr(96) 避免源码里出现反引号）。
_FENCE = chr(96) * 3

_DEFAULT_PATTERNS: dict[LeakCategory, tuple[re.Pattern[str], ...]] = {
    LeakCategory.FILE_NAME: (
        re.compile(
            r"(?<![\w./-])[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|json|ya?ml|md|rs|swift|m|mm|c|cc|cpp|h|hpp|go|java|kt|kts|sh|bash|html|css|scss|mp4|mov|png|jpe?g|svg|toml|lock)\b",
            re.IGNORECASE,
        ),
    ),
    LeakCategory.FUNCTION_NAME: (
        re.compile(r"\b[a-z_][a-z0-9_]{1,40}\(\)"),
        re.compile(r"\b(?:def|function|func|fn)\s+[A-Za-z_]\w*"),
    ),
    LeakCategory.API_NAME: (
        re.compile(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\([^)]*\)"),
        re.compile(r"\bAPI\b"),
    ),
    LeakCategory.LOG_TEXT: (
        re.compile(r"Traceback \(most recent call last\)"),
        re.compile(r'File ".*?", line \d+'),
        re.compile(r"\b(?:Exception|Error|ERROR|FATAL|panic|Segmentation fault)\b"),
        re.compile(r"\bexit code \d+"),
        re.compile(r"^\s*at\s+[\w.$]+\(.*\)", re.MULTILINE),
    ),
    LeakCategory.FIX_SUGGESTION: (
        re.compile(
            r"(?:建议|应该|可以试|试试|试着|需要把|改成|改为|修改|修复一下|替换成|加上|删掉|补上)"
        ),
        re.compile(
            r"\b(?:you should|try to|fix (?:it|this) by|change .{0,20} to|replace .{0,20} with)\b",
            re.IGNORECASE,
        ),
    ),
    LeakCategory.CODE: (
        re.compile(_FENCE),
        re.compile(
            r"(?:^|\n)\s*(?:def|class|function|func|fn|import|from\s+\S+\s+import|#include|using)\s"
        ),
        re.compile(r"(?:=>|===|!==)"),
    ),
    LeakCategory.ANSWER: (
        re.compile(
            r"(?:正确答案|标准答案|参考答案|参考解|reference answer|golden answer)", re.IGNORECASE
        ),
    ),
    LeakCategory.HIDDEN_TEST: (
        re.compile(r"(?:hidden[-_ ]?tests?|隐藏测试|test_[a-z0-9_]+\.py|run\.sh\b)", re.IGNORECASE),
    ),
    LeakCategory.INTERNAL_TERM: (
        re.compile(
            r"(?:rubric\s*(?:item|条目)|评分规则|判分规则|reward\s*文件|f1_symptom|f2_evidence|f3_visual|f3_assist)"
        ),
        re.compile(r"(?:扣分|档位规则|tier\s*[:=]|penalty\s*[:=])", re.IGNORECASE),
    ),
    LeakCategory.CANARY: (re.compile(r"CNRY-[0-9a-f]{8}-[0-9a-f]{8}", re.IGNORECASE),),
    LeakCategory.PROMPT_INJECTION: (
        re.compile(
            r"(?:ignore\s+(?:all\s+)?(?:previous|above)\s+instructions|忽略(?:上面|之前|以上)的?指令|system\s*prompt|系统提示词?)",
            re.IGNORECASE,
        ),
        re.compile(r"(?:给裁判|对裁判说|judge\s+should\s+pass)", re.IGNORECASE),
    ),
    LeakCategory.MULTIPLE_DEFECTS: (),
}


@dataclass(frozen=True)
class LeakViolation:
    """一条泄露/违规命中。"""

    category: LeakCategory
    detail: str
    matched: str = ""
    level: FeedbackLevel | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "category": self.category.value,
            "detail": self.detail,
            "matched": self.matched,
            "level": None if self.level is None else str(self.level),
        }


class LeakChecker(Protocol):
    """泄露检查器接口：可插拔正则、小模型或两者组合。"""

    def check(self, text: str, level: FeedbackLevel) -> list[LeakViolation]:
        """返回命中列表；空列表表示通过。"""
        ...


class RegexLeakChecker:
    """基于正则集的泄露检查器（F1-F3 禁止内容 + 任务自定义禁词）。

    Args:
        extra_patterns: 额外正则（category -> 模式串），用于题目自定义禁词。
        forbidden_terms: 参考答案/隐藏测试里的精确禁词，命中记 ANSWER。
    """

    def __init__(
        self,
        *,
        extra_patterns: Mapping[LeakCategory, Sequence[str]] | None = None,
        forbidden_terms: Iterable[str] = (),
    ) -> None:
        self._patterns = dict(_DEFAULT_PATTERNS)
        for category, patterns in (extra_patterns or {}).items():
            self._patterns[category] = (
                *self._patterns.get(category, ()),
                *(re.compile(pattern) for pattern in patterns),
            )
        self._forbidden_terms = tuple(term for term in forbidden_terms if term)

    def check(self, text: str, level: FeedbackLevel) -> list[LeakViolation]:
        """按级别禁止类别扫描文本，返回去重后的命中列表。"""
        forbidden = FORBIDDEN_BY_LEVEL[level]
        violations: list[LeakViolation] = []
        seen: set[tuple[str, str]] = set()
        for category in sorted(forbidden, key=lambda item: item.value):
            for pattern in self._patterns.get(category, ()):
                for match in pattern.finditer(text):
                    matched = match.group(0)
                    key = (category.value, matched)
                    if key in seen or not matched:
                        continue
                    seen.add(key)
                    violations.append(
                        LeakViolation(
                            category=category,
                            detail=f"{level} 级别禁止 {category.value}",
                            matched=matched[:120],
                            level=level,
                        )
                    )
        for term in self._forbidden_terms:
            if term and term in text:
                key = (LeakCategory.ANSWER.value, term)
                if key not in seen:
                    seen.add(key)
                    violations.append(
                        LeakViolation(
                            category=LeakCategory.ANSWER,
                            detail="命中题目自定义禁词（答案/隐藏测试）",
                            matched=term[:120],
                            level=level,
                        )
                    )
        return violations


class CallableLeakChecker:
    """把任意可调用对象适配成 LeakChecker（供模型检查器注入）。"""

    def __init__(
        self,
        fn: Callable[[str, FeedbackLevel], Iterable[LeakViolation]],
        *,
        name: str = "callable",
    ) -> None:
        self._fn = fn
        self.name = name

    def check(self, text: str, level: FeedbackLevel) -> list[LeakViolation]:
        """调用底层函数并规范化为 list。"""
        return list(self._fn(text, level))


class CompositeLeakChecker:
    """组合多个检查器（如正则 + 小模型），按顺序汇总命中。"""

    def __init__(self, checkers: Sequence[LeakChecker]) -> None:
        if not checkers:
            raise ValueError("CompositeLeakChecker 至少需要一个检查器")
        self._checkers = tuple(checkers)

    def check(self, text: str, level: FeedbackLevel) -> list[LeakViolation]:
        """汇总所有检查器的命中。"""
        violations: list[LeakViolation] = []
        for checker in self._checkers:
            violations.extend(checker.check(text, level))
        return violations


# ---------------------------------------------------------------------------
# 反馈历史、决策与消息
# ---------------------------------------------------------------------------


@dataclass
class FeedbackRound:
    """一次已经发送的反馈及其效果。

    made_progress 由下一轮观察回填：
      True  = 目标缺陷已修复/有进展
      False = 目标缺陷仍存在
      None  = 尚未观察（控制器按无进展保守处理）
    """

    round_no: int
    level: FeedbackLevel
    defect_ids: tuple[str, ...] = ()
    made_progress: bool | None = None
    progress: str | None = None
    text: str = ""

    def __post_init__(self) -> None:
        if self.round_no < 1:
            raise ValueError(f"反馈轮次从 1 开始，收到: {self.round_no!r}")
        if len(self.defect_ids) > 1:
            raise ValueError("每条反馈只讲一个缺陷：defect_ids 最多 1 个")
        if self.level == FeedbackLevel.F0 and self.defect_ids:
            raise ValueError("F0 不含任何具体缺陷")
        if (
            self.level in (FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F3)
            and not self.defect_ids
        ):
            raise ValueError(f"{self.level} 反馈必须指向恰好一个缺陷")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "round": self.round_no,
            "level": str(self.level),
            "defect_ids": list(self.defect_ids),
            "made_progress": self.made_progress,
            "progress": self.progress,
            "text": self.text,
        }


@dataclass(frozen=True)
class FeedbackDecision:
    """控制器的下一步反馈决策。"""

    level: FeedbackLevel
    defect_id: str | None
    allowed_fields: frozenset[str]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "level": str(self.level),
            "defect_id": self.defect_id,
            "allowed_fields": sorted(self.allowed_fields),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FeedbackValidation:
    """一条反馈文本的检查结果。"""

    ok: bool
    violations: tuple[LeakViolation, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "violations": [violation.to_dict() for violation in self.violations],
        }


def validate_feedback_message(
    *,
    level: FeedbackLevel,
    defect_ids: Sequence[str],
    text: str,
    checker: LeakChecker,
) -> FeedbackValidation:
    """校验一条待发送反馈：单缺陷约束 + 泄露检查。

    模拟用户生成文本后必须先过本函数；ok=False 时要求重新生成。
    """
    violations: list[LeakViolation] = []
    if level == FeedbackLevel.F0:
        if defect_ids:
            violations.append(
                LeakViolation(
                    category=LeakCategory.MULTIPLE_DEFECTS,
                    detail="F0 不允许提及任何具体缺陷",
                    level=level,
                )
            )
    elif len(defect_ids) != 1:
        violations.append(
            LeakViolation(
                category=LeakCategory.MULTIPLE_DEFECTS,
                detail=f"{level} 消息必须恰好指向一个缺陷，实际 {len(defect_ids)} 个",
                level=level,
            )
        )
    violations.extend(checker.check(text, level))
    if violations:
        return FeedbackValidation(
            ok=False, violations=tuple(violations), reason="泄露检查未通过，需重写"
        )
    return FeedbackValidation(ok=True, reason="")


# ---------------------------------------------------------------------------
# 反馈阶梯控制器
# ---------------------------------------------------------------------------


def _severity_priority(severity: Severity) -> int:
    """缺陷优先级：fatal(0) > functional(1) > minor(2)。"""
    return {Severity.FATAL: 0, Severity.FUNCTIONAL: 1, Severity.MINOR: 2}[severity]


class FeedbackLadder:
    """F0-F3 反馈阶梯控制器。

    控制器本身无状态：输入缺陷跟踪状态 + 历史 + 预算，输出下一轮级别与
    允许字段；所有历史由调用方持有（FeedbackRound 列表）。
    """

    def __init__(
        self,
        escalation: EscalationPolicy | None = None,
        *,
        max_f1_no_progress: int | None = None,
        max_f2_no_progress: int | None = None,
    ) -> None:
        policy = escalation or EscalationPolicy()
        self.escalation = EscalationPolicy(
            f1_no_progress_rounds=(
                policy.f1_no_progress_rounds if max_f1_no_progress is None else max_f1_no_progress
            ),
            f2_no_progress_rounds=(
                policy.f2_no_progress_rounds if max_f2_no_progress is None else max_f2_no_progress
            ),
        )
        self.max_f1_no_progress = self.escalation.f1_no_progress_rounds
        self.max_f2_no_progress = self.escalation.f2_no_progress_rounds

    def select_target(self, tracker: DefectTracker) -> Defect | None:
        """只从未修复的 functional/fatal 缺陷中选目标（docs/03 §7.4）。

        minor 缺陷永不触发反馈（它们不阻断成功条件，只影响 P/A 与半档）。
        """
        blocking = tracker.blocking_open()
        if not blocking:
            return None
        return min(
            blocking,
            key=lambda defect: (
                _severity_priority(defect.severity),
                defect.first_round,
                defect.defect_id,
            ),
        )

    def no_progress_count(
        self,
        history: Sequence[FeedbackRound],
        defect_id: str,
        level: FeedbackLevel,
    ) -> int:
        """某缺陷在指定级别上"连续无进展"的轮数。

        从最新一轮向前数，遇到有进展或不同级别即停止；made_progress=None
        视为无进展（保守策略，避免过早升级）。
        """
        count = 0
        for entry in reversed(history):
            if defect_id not in entry.defect_ids:
                continue
            if entry.level != level:
                break
            if entry.made_progress is True or entry.progress in ("partial", "fixed"):
                break
            count += 1
        return count

    def next_decision(
        self,
        tracker: DefectTracker,
        history: Sequence[FeedbackRound],
        budget: Budget,
        usage: BudgetUsage,
    ) -> FeedbackDecision | None:
        """计算下一轮反馈决策；返回 None 表示应停止反馈。

        停止条件见 stop_reason()：无未修复缺陷或预算耗尽。
        """
        if usage.feedback_rounds >= budget.max_feedback_rounds:
            return None
        if not usage.can_continue(budget):
            return None
        target = self.select_target(tracker)
        if target is None:
            return None
        history_for_target = [entry for entry in history if target.id in entry.defect_ids]
        if not history_for_target:
            return FeedbackDecision(
                level=FeedbackLevel.F1,
                defect_id=target.id,
                allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F1],
                reason=f"缺陷 {target.defect_id} 首次反馈，从最低级别 F1 开始",
            )
        last_level = history_for_target[-1].level
        if last_level == FeedbackLevel.F1:
            no_progress = self.no_progress_count(history, target.id, FeedbackLevel.F1)
            if no_progress >= self.max_f1_no_progress:
                return FeedbackDecision(
                    level=FeedbackLevel.F2,
                    defect_id=target.id,
                    allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F2],
                    reason=(
                        f"缺陷 {target.defect_id} 在 F1 连续 {no_progress} 轮无进展，"
                        "升级到 F2（提供日志/报错原文）"
                    ),
                )
            return FeedbackDecision(
                level=FeedbackLevel.F1,
                defect_id=target.id,
                allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F1],
                reason=f"缺陷 {target.defect_id} F1 尚未达到升级阈值（{no_progress}/{self.max_f1_no_progress}）",
            )
        if last_level == FeedbackLevel.F2:
            no_progress = self.no_progress_count(history, target.id, FeedbackLevel.F2)
            if no_progress >= self.max_f2_no_progress:
                return FeedbackDecision(
                    level=FeedbackLevel.F3,
                    defect_id=target.id,
                    allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F3],
                    reason=(
                        f"缺陷 {target.defect_id} 在 F2 连续 {no_progress} 轮无进展，"
                        "升级到 F3（视觉描述/协助操作）"
                    ),
                )
            return FeedbackDecision(
                level=FeedbackLevel.F2,
                defect_id=target.id,
                allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F2],
                reason=f"缺陷 {target.defect_id} 维持 F2",
            )
        # F3 已是最高级别，维持 F3 直到修复或预算耗尽。
        return FeedbackDecision(
            level=FeedbackLevel.F3,
            defect_id=target.id,
            allowed_fields=ALLOWED_FIELDS[FeedbackLevel.F3],
            reason=f"缺陷 {target.defect_id} 已用最高级别 F3，维持到修复或预算耗尽",
        )

    def stop_reason(
        self,
        tracker: DefectTracker,
        history: Sequence[FeedbackRound],
        budget: Budget,
        usage: BudgetUsage,
    ) -> str | None:
        """返回停止反馈的原因；仍可继续时返回 None。"""
        if self.select_target(tracker) is None:
            return "no_blocking_defects"
        reasons = usage.exhausted_reasons(budget)
        if reasons:
            return "budget_exhausted:" + ",".join(reasons)
        return None


# ---------------------------------------------------------------------------
# F0 特殊场景：试用、催继续、澄清问答
# ---------------------------------------------------------------------------


class F0Policy:
    """F0 场景的固定话术与次数限制。

    依据 plan-v1 §4.3 与 §8：
    - "我试用了一下" / 直接结束属于 F0，不含任何具体问题；
    - Agent 没做完就宣称完成或反复问"要不要继续"时，回一句不含信息的
      "请按你的判断做完"，计 F0，有次数上限；
    - 合理的澄清提问按题目 FAQ 回答，不算提示、不降档。
    """

    GENERIC_CONTINUE = "请按你的判断做完。"
    GENERIC_END = "好的，先到这里。"
    GENERIC_ANSWER = "请按题目要求自行决定。"
    PROBE = "我试用了一下。"

    def __init__(
        self,
        *,
        budget: Budget | None = None,
        max_nudges: int | None = None,
        faq: Mapping[str, str] | None = None,
    ) -> None:
        if max_nudges is None:
            max_nudges = budget.max_nudges if budget is not None else 2
        if max_nudges < 0:
            raise ValueError("max_nudges 不能为负")
        self.max_nudges = max_nudges
        self.faq = {key: value for key, value in (faq or {}).items()}
        self.nudges_used = 0
        self.question_count = 0

    def continue_prompt(self) -> str | None:
        """返回催继续话术；超过次数上限返回 None（调用方应结束 trial）。

        F0 无信息量催促不计反馈轮数、不降档（00-conventions §4.8 的
        max_nudges 字段；docs/02 §5.3 的 idle 停止原因）。
        """
        if self.nudges_used >= self.max_nudges:
            return None
        self.nudges_used += 1
        return self.GENERIC_CONTINUE

    @property
    def remaining_nudges(self) -> int:
        """剩余的无信息量催促次数。"""
        return max(0, self.max_nudges - self.nudges_used)

    def end_message(self) -> str:
        """结束语（F0）。"""
        return self.GENERIC_END

    def probe_message(self) -> str:
        """无反馈试用语（F0）。"""
        return self.PROBE

    def respond_to_question(self, question: str) -> str:
        """按 FAQ 回答澄清提问；未命中时给不含信息的通用回答。

        澄清回答不计入 F0 催继续次数，也不构成降档（plan-v1 §8）。
        """
        self.question_count += 1
        normalized = re.sub(r"[\s，。！？,.!?：:；;]+", "", question).lower()
        for key, answer in self.faq.items():
            if key and re.sub(r"\s+", "", key).lower() in normalized:
                return answer
        return self.GENERIC_ANSWER


# ---------------------------------------------------------------------------
# 预算记账辅助
# ---------------------------------------------------------------------------


def consume_decision(
    usage: BudgetUsage,
    decision: FeedbackDecision,
    *,
    user_cost_usd: float = 0.0,
) -> BudgetUsage:
    """按决策记账：F1-F3 计一次反馈轮，F0 计一次 nudge（不降档）。"""
    if decision.level == FeedbackLevel.F0:
        usage.add_nudge(user_cost_usd=user_cost_usd)
    else:
        usage.add_feedback_round(user_cost_usd=user_cost_usd)
    return usage


# ---------------------------------------------------------------------------
# feedback.schema.json 记录构造
# ---------------------------------------------------------------------------

_FEEDBACK_TEMPLATE_PATTERN = re.compile(r"^F[0-3]-[A-Z0-9-]{2,32}$")


def feedback_kind(level: FeedbackLevel) -> str:
    """F0 -> nudge（无信息量催促），F1-F3 -> feedback。"""
    return "nudge" if level == FeedbackLevel.F0 else "feedback"


def build_leak_check(
    level: FeedbackLevel,
    text: str,
    checker: LeakChecker | None,
    *,
    model: str = "",
    model_verdict: str = "clean",
    model_reasons: Sequence[str] = (),
    rewrite_count: int = 0,
    fallback_used: bool = False,
) -> dict[str, Any]:
    """构造 feedback.schema.json 的 leak_check 对象。

    Args:
        checker: 正则/组合/模型检查器；None 表示只做单缺陷约束（不推荐上线用）。
        model: 小模型复核的模型 slug（可选）。
        model_verdict: clean / suspect / leak。
        model_reasons: 复核理由。
        rewrite_count: 重写次数（0-3）；3 次仍失败应 final_action=blocked。
        fallback_used: 是否退回预写模板原文。
    """
    violations = checker.check(text, level) if checker is not None else []
    categories = sorted({violation.category.value for violation in violations})
    rule_ids = sorted({LEAK_RULE_IDS[violation.category] for violation in violations})
    passed = not violations and model_verdict == "clean"
    rewrite = max(0, min(3, int(rewrite_count)))
    final_action = "send" if passed and rewrite < 3 else "blocked"
    return {
        "passed": passed,
        "rule_ids_hit": rule_ids,
        "categories_hit": categories,
        "model_check": {
            "model": model,
            "verdict": model_verdict,
            "reasons": list(model_reasons),
        },
        "rewrite_count": rewrite,
        "fallback_used": bool(fallback_used),
        "final_action": final_action,
    }


def budget_snapshot(budget: Budget, usage: BudgetUsage) -> dict[str, Any]:
    """构造 feedback.schema.json 的 budget_snapshot（字段名与 00-conventions §4.8 一致）。"""
    return {
        "feedback_rounds_used": usage.feedback_rounds,
        "max_feedback_rounds": budget.max_feedback_rounds,
        "nudges_used": usage.nudges,
        "max_nudges": budget.max_nudges,
        "tokens_used": usage.total_tokens,
        "max_total_tokens": budget.max_total_tokens,
        "wall_clock_s": round(usage.wall_clock_s, 3),
        "max_wall_clock_s": budget.max_wall_clock_s,
        "cost_usd_used": round(usage.agent_cost_usd, 6),
        "max_cost_usd": budget.max_cost_usd,
        "exhausted": not usage.can_continue(budget),
        "exhausted_by": usage.exhausted_by(budget),
    }


@dataclass
class FeedbackRecord:
    """一条反馈决策记录（feedback.schema.json 的运行时镜像）。

    kind=nudge 时 level 必须是 F0、target_defect_id 为 None、allowed_fields 为空、
    text ≤ 60 字；kind=feedback 时 level ∈ {F1,F2,F3} 且恰好一个缺陷。
    """

    trial_id: str
    feedback_index: int
    level: FeedbackLevel
    text: str
    template_id: str
    decided_at: str
    budget: dict[str, Any]
    leak_check: dict[str, Any]
    target_defect_id: str | None = None
    allowed_fields: tuple[str, ...] = ()
    generation: dict[str, Any] | None = None
    escalation: tuple[dict[str, Any], ...] = ()
    delivery_status: str = "sent"
    sent_at: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 1 <= self.feedback_index <= 99:
            raise ValueError("feedback_index 范围 1..99")
        feedback_kind(self.level)
        if self.level == FeedbackLevel.F0:
            if self.target_defect_id is not None:
                raise ValueError("nudge 的 target_defect_id 必须为 null")
            if self.allowed_fields:
                raise ValueError("nudge 的 allowed_fields 必须为空")
            if len(self.text) > 60:
                raise ValueError("nudge 文本不得超过 60 字（schema maxLength=60）")
        else:
            if self.target_defect_id is None:
                raise ValueError("feedback 必须恰好指向一个缺陷")
            if not self.allowed_fields:
                raise ValueError("feedback 的 allowed_fields 至少 1 个")
            extra = set(self.allowed_fields) - set(ALLOWED_FIELDS[self.level])
            if extra:
                raise ValueError(f"{self.level} 不允许字段: {sorted(extra)}")
        if not _FEEDBACK_TEMPLATE_PATTERN.match(self.template_id):
            raise ValueError(f"template_id 不符合 F[0-3]-[A-Z0-9-]{{2,32}}: {self.template_id!r}")
        if self.delivery_status not in ("sent", "blocked"):
            raise ValueError("delivery_status 只能是 sent 或 blocked")
        if self.delivery_status == "blocked" and self.leak_check.get("final_action") != "blocked":
            raise ValueError("delivery=blocked 时 leak_check.final_action 必须也是 blocked")
        self.allowed_fields = tuple(self.allowed_fields)
        self.escalation = tuple(self.escalation)

    @property
    def kind(self) -> str:
        """feedback 或 nudge（由 level 推导）。"""
        return feedback_kind(self.level)

    @property
    def text_hash(self) -> str:
        """text 的 sha256（用于审计与重放）。"""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """序列化为 feedback.schema.json 对象。"""
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "trial_id": self.trial_id,
            "feedback_index": self.feedback_index,
            "kind": self.kind,
            "level": str(self.level),
            "target_defect_id": self.target_defect_id,
            "allowed_fields": list(self.allowed_fields),
            "text": self.text,
            "text_hash": self.text_hash,
            "template_id": self.template_id,
            "leak_check": dict(self.leak_check),
            "budget": dict(self.budget),
            "decided_at": self.decided_at,
            "delivery": {
                "status": self.delivery_status,
                "sent_at": self.sent_at,
            },
        }
        if self.generation is not None:
            payload["generation"] = dict(self.generation)
        if self.escalation:
            payload["escalation"] = [dict(item) for item in self.escalation]
        if self.extensions:
            payload["extensions"] = dict(self.extensions)
        return payload


def build_feedback_record(
    *,
    trial_id: str,
    feedback_index: int,
    decision: FeedbackDecision,
    text: str,
    template_id: str,
    decided_at: str,
    budget: Budget,
    usage: BudgetUsage,
    checker: LeakChecker | None = None,
    generation: dict[str, Any] | None = None,
    escalation: Sequence[dict[str, Any]] = (),
    rewrite_count: int = 0,
    fallback_used: bool = False,
    blocked: bool = False,
    sent_at: str | None = None,
    model: str = "",
    model_verdict: str = "clean",
    model_reasons: Sequence[str] = (),
) -> FeedbackRecord:
    """由控制器决策 + 已生成文本构造一条 feedback.schema.json 记录。

    blocked=True 表示泄露重写 3 次仍失败或人工阻断：delivery.status=blocked，
    调用方应把 trial 转 verifying 并记 feedback_blocked（docs/02 §4.2）。
    """
    leak_check = build_leak_check(
        decision.level,
        text,
        checker,
        model=model,
        model_verdict=model_verdict,
        model_reasons=model_reasons,
        rewrite_count=rewrite_count,
        fallback_used=fallback_used,
    )
    if blocked:
        leak_check["final_action"] = "blocked"
        leak_check["passed"] = False
    delivery_status = "blocked" if blocked else "sent"
    return FeedbackRecord(
        trial_id=trial_id,
        feedback_index=feedback_index,
        level=decision.level,
        target_defect_id=decision.defect_id,
        allowed_fields=tuple(sorted(decision.allowed_fields)),
        text=text,
        template_id=template_id,
        decided_at=decided_at,
        budget=budget_snapshot(budget, usage),
        leak_check=leak_check,
        generation=generation,
        escalation=tuple(escalation),
        delivery_status=delivery_status,
        sent_at=None if blocked else (sent_at or decided_at),
    )
