"""evalspleef.core.state — trial 状态机、终态判定与重判（grade 版本）管理。

事实来源：docs/02-eval-flow.md §4.2（合法转移表）、§6（终态优先级 P0-P5）、
§8（重试/重判/作废语义），以及 docs/00-conventions.md §4.1/§4.7。

要点
----
- 状态机是事件驱动的：调用方只发事件（英文 snake_case），不直接改 state；
- 非法转移抛 IllegalTransition，消息里给出当前状态与允许事件；
- failed_infra 是中间态：requeue 回 pending（重试预算最多 3 次）或 invalidate；
- scored 不是绝对终态：事后隔离审计命中可 invalidate_post_hoc -> invalid；
- paused_manual 的恢复目标按暂停前状态记忆，同时显式支持
  resume_running / resume_observing / resume_verifying 三条人工恢复路径
  （00-conventions 只固定拼写，具体语义见 docs/02 §4.2）；
- 重判不新建 trial，只在 trial.grades[] 追加新版本；人工覆盖同样产生新版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    DecidedBy,
    Grade,
    GradeStatus,
    HumanOverride,
    TrialRecord,
    TrialState,
    Validity,
)

__all__ = [
    "EVENT_DESCRIPTIONS",
    "LEGAL_TRANSITIONS",
    "MAX_INFRA_RETRIES",
    "TERMINAL_STATES",
    "IllegalTransition",
    "RegradeNotAllowed",
    "StateEvent",
    "TerminalDecision",
    "TrialMachine",
    "apply_manual_override",
    "apply_terminal",
    "describe_transitions",
    "evaluate_terminal",
    "event_path_for_terminal",
    "record_grade",
    "regrade",
]

#: docs/02 §4.2 合法转移表（唯一事实来源）。
_LEGAL_TRANSITIONS: dict[TrialState, dict[str, TrialState]] = {
    TrialState.PENDING: {
        "lease": TrialState.LEASED,
        "invalidate": TrialState.INVALID,
    },
    TrialState.LEASED: {
        "start": TrialState.RUNNING,
        "fail_infra": TrialState.FAILED_INFRA,
    },
    TrialState.RUNNING: {
        "stop": TrialState.OBSERVING,
        "fail_infra": TrialState.FAILED_INFRA,
        "pause": TrialState.PAUSED_MANUAL,
    },
    TrialState.OBSERVING: {
        "needs_feedback": TrialState.AWAITING_FEEDBACK,
        "finish": TrialState.VERIFYING,
    },
    TrialState.AWAITING_FEEDBACK: {
        "deliver": TrialState.RUNNING,
        # awaiting_feedback -> verifying 的三种触发：
        # 1) 泄露重写 3 次仍失败（feedback_blocked）；
        # 2) 预算耗尽（budget_exhausted，docs/02 §6 P3）；
        # 3) 不可继续（finish，docs/02 §6 P4）。
        "feedback_blocked": TrialState.VERIFYING,
        "budget_exhausted": TrialState.VERIFYING,
        "finish": TrialState.VERIFYING,
    },
    TrialState.VERIFYING: {
        "score": TrialState.SCORED,
        "pause": TrialState.PAUSED_MANUAL,
        "fail_infra": TrialState.FAILED_INFRA,
    },
    TrialState.PAUSED_MANUAL: {
        "resume_running": TrialState.RUNNING,
        "resume_observing": TrialState.OBSERVING,
        "resume_verifying": TrialState.VERIFYING,
        "invalidate": TrialState.INVALID,
    },
    TrialState.SCORED: {
        "invalidate_post_hoc": TrialState.INVALID,
    },
    TrialState.FAILED_INFRA: {
        "requeue": TrialState.PENDING,
        "invalidate": TrialState.INVALID,
    },
    TrialState.INVALID: {},
}

#: 只读视图；模块内不修改，避免调用方误改。
LEGAL_TRANSITIONS: dict[TrialState, dict[str, TrialState]] = {
    state: dict(events) for state, events in _LEGAL_TRANSITIONS.items()
}

#: 终态（docs/02 §4.2）：scored、invalid。failed_infra 是中间态，不是终态。
TERMINAL_STATES: frozenset[TrialState] = frozenset({TrialState.SCORED, TrialState.INVALID})

#: failed_infra -> pending 的最大重试次数（docs/02 §4.2 守卫条件）。
MAX_INFRA_RETRIES = 3

#: 事件说明，供文档、错误消息与 CLI 轨迹使用。
EVENT_DESCRIPTIONS: dict[str, str] = {
    "lease": "调度器派发租约（资源可用、版本冻结、预算未耗尽、cell 未 Skip）",
    "start": "预检通过（镜像 digest、金丝雀注入、I1-I9），启动外壳",
    "stop": "Agent 停止，冻结沙箱并进入观察",
    "needs_feedback": "观察完成且未达终态，进入反馈决策",
    "deliver": "反馈已过泄露检查并发送，回到 running 开启下一轮",
    "feedback_blocked": "反馈 3 次重写仍泄露，转 verifying 按未通过判分",
    "budget_exhausted": "预算耗尽，观察后直接转 verifying（Failed）",
    "finish": "观察/反馈阶段结束，进入独立验证",
    "score": "隐藏测试 + 裁判完成，写入 grade v1",
    "fail_infra": "基础设施异常或预检失败，进入 failed_infra（可重排）",
    "requeue": "重试预算未耗尽，重新排队等待租约",
    "invalidate": "作废（计划取消/重试耗尽/人工判定）",
    "invalidate_post_hoc": "事后隔离审计命中或版本作废，scored -> invalid",
    "pause": "人工中断，保留沙箱等待恢复",
    "resume": "人工恢复（按暂停前状态，或显式 resume_* 指定目标）",
    "resume_running": "人工恢复：回到 running",
    "resume_observing": "人工恢复：回到 observing",
    "resume_verifying": "人工恢复：回到 verifying",
}

#: 进入各状态时对 validity 的影响；未列出的转移保持原值。
_VALIDITY_ON_ENTER: dict[TrialState, Validity] = {
    TrialState.INVALID: Validity.INVALID,
    TrialState.FAILED_INFRA: Validity.INVALID,
    TrialState.PAUSED_MANUAL: Validity.MANUAL,
    TrialState.SCORED: Validity.VALID,
    TrialState.PENDING: Validity.PENDING,
}

_RESUME_EVENTS: dict[TrialState, str] = {
    TrialState.RUNNING: "resume_running",
    TrialState.OBSERVING: "resume_observing",
    TrialState.VERIFYING: "resume_verifying",
}


class IllegalTransition(ValueError):
    """非法状态转移。

    Attributes:
        from_state: 转移前的状态。
        event: 触发的事件名。
        allowed: 当前状态下允许的事件元组。
    """

    def __init__(self, from_state: TrialState, event: str, allowed: tuple[str, ...]) -> None:
        self.from_state = from_state
        self.event = event
        self.allowed = allowed
        super().__init__(
            f"trial 状态 {from_state} 不允许事件 {event!r}；"
            f"允许的事件: {', '.join(allowed) if allowed else '（终态，无）'}"
        )


class RegradeNotAllowed(ValueError):
    """未到 scored 状态或 grade 版本冲突时尝试重判。"""


@dataclass(frozen=True)
class StateEvent:
    """一次状态转移的审计记录。"""

    seq: int
    event: str
    from_state: TrialState
    to_state: TrialState
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "seq": self.seq,
            "event": self.event,
            "from_state": str(self.from_state),
            "to_state": str(self.to_state),
            "note": self.note,
        }


class TrialMachine:
    """单个 trial 的事件驱动状态机。

    Example:
        machine = TrialMachine("web-001.demo.model.shell.e1.t01")
        machine.apply("lease"); machine.apply("start")
        machine.apply("stop"); machine.apply("needs_feedback")
        machine.apply("deliver"); machine.apply("stop")
        machine.apply("finish"); machine.apply("score")
        machine.record.state is TrialState.SCORED
    """

    def __init__(
        self,
        trial_id: str,
        *,
        state: TrialState = TrialState.PENDING,
        record: TrialRecord | None = None,
        max_infra_retries: int = MAX_INFRA_RETRIES,
    ) -> None:
        self.record = record if record is not None else TrialRecord(trial_id=trial_id)
        self.record.state = state
        self.history: list[StateEvent] = []
        self._resume_state: TrialState | None = None
        self._infra_failures = 0
        self._requeues = 0
        self.max_infra_retries = max_infra_retries
        if state == TrialState.PAUSED_MANUAL:
            self._resume_state = TrialState.RUNNING

    @property
    def trial_id(self) -> str:
        """trial ID。"""
        return self.record.trial_id

    @property
    def state(self) -> TrialState:
        """当前状态。"""
        return self.record.state

    @property
    def validity(self) -> Validity:
        """当前成绩有效性。"""
        return self.record.validity

    @property
    def infra_failures(self) -> int:
        """已发生的 failed_infra 次数。"""
        return self._infra_failures

    @property
    def requeues(self) -> int:
        """已重新排队的次数。"""
        return self._requeues

    def allowed_events(self) -> tuple[str, ...]:
        """当前状态下允许的事件。

        paused_manual 额外接受通用 resume 别名（等价于按暂停前状态恢复）。
        """
        events = tuple(LEGAL_TRANSITIONS[self.state])
        if self.state == TrialState.PAUSED_MANUAL:
            events = (*events, "resume")
        if self.state == TrialState.FAILED_INFRA and self._requeues >= self.max_infra_retries:
            # 重试预算耗尽：requeue 不再合法，只剩 invalidate。
            events = tuple(event for event in events if event != "requeue")
        return events

    def can(self, event: str) -> bool:
        """事件在当前状态下是否合法。"""
        return event in self.allowed_events()

    def apply(self, event: str, *, note: str = "") -> TrialState:
        """应用一个事件并返回新状态。

        Raises:
            IllegalTransition: 当前状态不允许该事件，或重试预算已耗尽。
        """
        from_state = self.state
        allowed = self.allowed_events()
        if event not in allowed:
            raise IllegalTransition(from_state, event, allowed)

        if event == "resume":
            target = self._resume_state or TrialState.RUNNING
            event = _RESUME_EVENTS[target]
        to_state = LEGAL_TRANSITIONS[from_state][event]

        if event == "pause":
            self._resume_state = from_state
        elif event in _RESUME_EVENTS.values():
            self._resume_state = None
        if event == "fail_infra":
            self._infra_failures += 1
        if event == "requeue":
            self._requeues += 1

        self.record.state = to_state
        if to_state in _VALIDITY_ON_ENTER:
            self.record.validity = _VALIDITY_ON_ENTER[to_state]
        elif from_state == TrialState.PAUSED_MANUAL:
            # 从人工暂停恢复：有效性回到待定，直到后续 score/invalidate 再落定。
            self.record.validity = Validity.PENDING
        self.history.append(
            StateEvent(
                seq=len(self.history) + 1,
                event=event,
                from_state=from_state,
                to_state=to_state,
                note=note,
            )
        )
        return to_state

    def snapshot(self) -> dict[str, Any]:
        """返回 trial 记录 + 转移历史的可 JSON 快照。"""
        snapshot = self.record.to_dict()
        snapshot["history"] = [event.to_dict() for event in self.history]
        return snapshot


# ---------------------------------------------------------------------------
# 终态判定（docs/02 §6 优先级 P0-P5，短路取第一条）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalDecision:
    """一次终态判定的结果。

    code: 优先级编号 P0-P5。
    terminal_state: 最终应进入的状态（invalid 或 verifying）。
    validity: 终态下的成绩有效性。
    scoring: True 表示应走 verifying -> scored 出分；False 表示作废。
    reason: 判定原因（写入事件/报告）。
    """

    code: str
    terminal_state: TrialState
    validity: Validity
    scoring: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "code": self.code,
            "terminal_state": str(self.terminal_state),
            "validity": str(self.validity),
            "scoring": self.scoring,
            "reason": self.reason,
        }


def evaluate_terminal(
    *,
    isolation_failed: bool = False,
    cheat_detected: bool = False,
    hard_gates_passed: bool = False,
    has_unresolved_blocking_defect: bool = False,
    budget_exhausted: bool = False,
    cannot_continue: bool = False,
    manual_interrupt_expired: bool = False,
) -> TerminalDecision | None:
    """按 docs/02 §6 的优先级 P0-P5 判定终态；未达终态返回 None。

    Args:
        isolation_failed: P0，基础设施/隔离失效（金丝雀跨 trial、预检失败、宿主污染）。
        cheat_detected: P1，作弊命中（改测试/写 reward/硬编码/查答案/绕过网络/注入裁判）。
        hard_gates_passed: P2 的必要条件之一。
        has_unresolved_blocking_defect: P2 的必要条件之一（存在未修复的 functional/fatal 缺陷）。
        budget_exhausted: P3，轮数/墙钟/token/费用耗尽。
        cannot_continue: P4，反馈无法生成、Agent 连续崩溃但非环境问题。
        manual_interrupt_expired: P5，人工中断 30 min 未恢复。
    """
    if isolation_failed:
        return TerminalDecision(
            code="P0",
            terminal_state=TrialState.INVALID,
            validity=Validity.INVALID,
            scoring=False,
            reason="基础设施/隔离失效，作废重跑",
        )
    if cheat_detected:
        return TerminalDecision(
            code="P1",
            terminal_state=TrialState.VERIFYING,
            validity=Validity.VALID,
            scoring=True,
            reason="作弊命中：判 0 + 标记 + 人工确认",
        )
    if hard_gates_passed and not has_unresolved_blocking_defect:
        return TerminalDecision(
            code="P2",
            terminal_state=TrialState.VERIFYING,
            validity=Validity.VALID,
            scoring=True,
            reason="硬门槛全过且无未修复 functional/fatal 缺陷，进入终判",
        )
    if budget_exhausted:
        return TerminalDecision(
            code="P3",
            terminal_state=TrialState.VERIFYING,
            validity=Validity.VALID,
            scoring=True,
            reason="预算耗尽（轮数/墙钟/token/费用），判 Failed",
        )
    if cannot_continue:
        return TerminalDecision(
            code="P4",
            terminal_state=TrialState.VERIFYING,
            validity=Validity.VALID,
            scoring=True,
            reason="不可继续（反馈无法生成/连续崩溃非环境问题），判 Failed",
        )
    if manual_interrupt_expired:
        return TerminalDecision(
            code="P5",
            terminal_state=TrialState.INVALID,
            validity=Validity.INVALID,
            scoring=False,
            reason="人工中断 30 min 未恢复，作废重跑",
        )
    return None


def event_path_for_terminal(machine: TrialMachine, decision: TerminalDecision) -> tuple[str, ...]:
    """返回从当前状态走到 decision.terminal_state 需要的事件序列。

    只覆盖 docs/02 §4.2 明示的路径；observing/awaiting_feedback 直接作废
    不在转移表中，调用方必须先走 failed_infra 或事后审计路径。
    """
    state = machine.state
    if decision.terminal_state == TrialState.SCORED:
        return ("score",)
    if decision.terminal_state == TrialState.VERIFYING:
        if state == TrialState.VERIFYING:
            return ()
        if state == TrialState.OBSERVING:
            return ("finish",)
        if state == TrialState.AWAITING_FEEDBACK:
            event = "feedback_blocked" if "feedback" in decision.reason else "finish"
            return (event,)
        raise IllegalTransition(state, "finish", machine.allowed_events())
    # INVALID
    if state == TrialState.PENDING:
        return ("invalidate",)
    if state in (TrialState.LEASED, TrialState.RUNNING, TrialState.VERIFYING):
        return ("fail_infra", "invalidate")
    if state == TrialState.SCORED:
        return ("invalidate_post_hoc",)
    if state == TrialState.FAILED_INFRA:
        return ("invalidate",)
    if state == TrialState.PAUSED_MANUAL:
        return ("invalidate",)
    raise IllegalTransition(state, "invalidate", machine.allowed_events())


def apply_terminal(machine: TrialMachine, decision: TerminalDecision) -> TrialState:
    """把终态判定落到状态机上（执行必要事件序列并返回新状态）。"""
    for event in event_path_for_terminal(machine, decision):
        machine.apply(event, note=decision.reason)
    return machine.state


# ---------------------------------------------------------------------------
# grade 版本管理（docs/02 §8：重判不新建 trial）
# ---------------------------------------------------------------------------


def record_grade(record: TrialRecord, grade: Grade) -> Grade:
    """把首个 grade 版本写入 trial 记录（要求已到 scored 状态）。

    Raises:
        RegradeNotAllowed: trial 尚未 scored 或已经有 grade。
    """
    if record.state != TrialState.SCORED:
        raise RegradeNotAllowed(
            f"trial {record.trial_id} 当前状态 {record.state}，只有 scored 后才能写 grade"
        )
    if record.grades:
        raise RegradeNotAllowed(f"trial {record.trial_id} 已有 grade，请用 regrade() 追加新版本")
    grade.grade_version = 1
    grade.supersedes = None
    grade.decided_by = DecidedBy.AUTO
    grade.status = GradeStatus.FINAL
    record.grades.append(grade)
    return grade


def regrade(record: TrialRecord, grade: Grade, *, reason: str = "") -> Grade:
    """为已 scored 的 trial 追加一个新 grade 版本（自动重判）。

    新版本号 = 已有 grade 数 + 1，supersedes = 上一版本号；
    decided_by 保留调用方传入值（默认 auto）。

    Raises:
        RegradeNotAllowed: trial 尚未 scored 或还没有首版 grade。
    """
    if record.state != TrialState.SCORED:
        raise RegradeNotAllowed(
            f"trial {record.trial_id} 当前状态 {record.state}，只有 scored 后才会出现重判"
        )
    if not record.grades:
        raise RegradeNotAllowed(f"trial {record.trial_id} 还没有首版 grade，请先 record_grade()")
    previous = record.grades[-1]
    previous.status = GradeStatus.SUPERSEDED
    grade.grade_version = len(record.grades) + 1
    grade.supersedes = previous.grade_version
    if reason:
        note = f"regrade: {reason}"
        grade.manual_notes = f"{grade.manual_notes}; {note}" if grade.manual_notes else note
    record.grades.append(grade)
    return grade


def apply_manual_override(
    record: TrialRecord,
    grade: Grade,
    *,
    reviewer: str,
    reason: str,
    at: str,
    decided_by: DecidedBy = DecidedBy.HUMAN,
    notes: str | None = None,
) -> Grade:
    """人工覆盖：产生 decided_by=human/arbiter、supersedes=旧版本号的新 grade 版本。

    Args:
        reviewer: 复核人/仲裁者 ID（human_override.reviewer）。
        reason: 覆盖理由（human_override.reason，schema 要求 ≥8 字符）。
        at: 覆盖时间（ISO-8601 UTC）。
        decided_by: human 或 arbiter。
        notes: 可选人工备注；缺省用 reason。
    """
    if decided_by not in (DecidedBy.HUMAN, DecidedBy.ARBITER):
        raise ValueError("人工覆盖的 decided_by 只能是 human 或 arbiter")
    if len(reason) < 8:
        raise ValueError("人工覆盖理由至少 8 个字符")
    previous = record.grades[-1] if record.grades else None
    grade.decided_by = decided_by
    grade.status = GradeStatus.FINAL
    grade.human_override = HumanOverride(
        reviewer=reviewer,
        reason=reason,
        at=at,
        previous_tier=previous.tier if previous is not None else None,
    )
    grade.manual_notes = notes or reason
    return regrade(record, grade, reason="manual override")


def describe_transitions() -> dict[str, dict[str, str]]:
    """返回状态转移表的可 JSON 描述，供文档与 CLI 使用。"""
    described: dict[str, dict[str, str]] = {}
    for state, events in LEGAL_TRANSITIONS.items():
        described[str(state)] = {event: str(target) for event, target in events.items()}
    return described
