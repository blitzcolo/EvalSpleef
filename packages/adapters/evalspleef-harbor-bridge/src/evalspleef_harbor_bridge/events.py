"""evalspleef.core.adapters.harbor — 与 Harbor 的薄适配层（唯一允许依赖 Harbor 的地方）。

docs/01-architecture.md §8 要求：所有 Harbor/Inspect 交互收敛到
reference/evalspleef.core/adapters/，升级只改这一层。本模块不 import harbor 包
（离线环境无法安装），只定义事件/错误策略的映射契约与纯函数，供线上适配器
在 Harbor 环境中接线。

事实来源：
- Harbor 错误策略单一事实表：
  https://docs.harborframework.com/jobs/error-classification.md
  （字段/异常类型会随 Harbor 版本变化，映射表按批次锁定版本，TODO(run-on-host) 校准）；
- docs/02-eval-flow.md §11 事件类型、§8 错误处置；
- docs/00-conventions.md §4.6 错误分类与处置。

使用方式（线上适配器）：
    adapter = HarborAdapter()
    for envelope in harbor_event_stream(batch):
        adapter.apply_event(envelope)
    for trial_id, machine in adapter.machines.items():
        store.save(machine.snapshot())
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from evalspleef.core.models import (
    ErrorClass,
    TrialError,
    TrialState,
)
from evalspleef.core.state import TrialMachine

__all__ = [
    "HARBOR_CAUSE_TO_ERROR_CLASS",
    "HARBOR_EVENT_TO_EVALKIT",
    "HarborAdapter",
    "error_from_harbor",
    "event_to_machine_event",
]

#: Harbor 错误 cause -> evalspleef.core ErrorClass（00-conventions §4.6）。
#: 依据 https://docs.harborframework.com/jobs/error-classification.md 的 cause 列。
HARBOR_CAUSE_TO_ERROR_CLASS: dict[str, ErrorClass] = {
    "agent_exhausted_budget": ErrorClass.MODEL,
    "agent_refused_task": ErrorClass.MODEL,
    "sandbox_failed": ErrorClass.ENVIRONMENT,
    "model_provider_failed": ErrorClass.PROVIDER,
    "harbor_failed": ErrorClass.ENVIRONMENT,
    "verifier_failed": ErrorClass.VERIFICATION,
}

#: Harbor 事件类型 -> evalspleef.core 状态机事件；None 表示只归档不改状态。
#: 事件类型清单见 docs/02-eval-flow.md §11。
HARBOR_EVENT_TO_EVALKIT: dict[str, str | None] = {
    "trial.created": None,
    "trial.leased": "lease",
    "sandbox.provisioned": None,
    "canary.injected": None,
    "preflight.result": None,
    "trial.started": "start",
    "agent.stopped": "stop",
    "observe.started": None,
    "feedback.sent": "deliver",
    "verify.started": "finish",
    "trial.scored": "score",
    "trial.invalidated": "invalidate",
    "teardown.completed": None,
}


def event_to_machine_event(event_type: str, payload: Mapping[str, Any] | None = None) -> str | None:
    """把 Harbor 事件映射为状态机事件；需要上下文的特例在此集中处理。"""
    payload = payload or {}
    if event_type == "observe.completed":
        # 观察完成：docs/02 §4.2 由控制器决定 needs_feedback 还是 finish。
        return "needs_feedback" if payload.get("needs_feedback") else "finish"
    if event_type == "feedback.blocked":
        return "feedback_blocked"
    if event_type == "trial.invalidated":
        # scored 之后的事后审计命中走 invalidate_post_hoc。
        return "invalidate_post_hoc" if payload.get("post_hoc") else "invalidate"
    if event_type == "failed_infra":
        return "fail_infra"
    return HARBOR_EVENT_TO_EVALKIT.get(event_type)


def error_from_harbor(
    cause: str,
    *,
    message: str = "",
    error_type: str = "",
) -> TrialError:
    """把 Harbor 的 (cause, exception type) 映射为 evalspleef.core TrialError。

    unknown/unclassified cause 按 environment + retry 处理（保守：不静默算模型错）。
    """
    error_class = HARBOR_CAUSE_TO_ERROR_CLASS.get(cause, ErrorClass.ENVIRONMENT)
    return TrialError(
        error_class=error_class,
        cause=cause,
        message=message or error_type,
    )


@dataclass
class HarborAdapter:
    """事件流 -> TrialMachine 的薄适配器。

    只做两件事：把 Harbor 事件映射成状态机事件、把错误 cause 映射成 ErrorClass；
    不复制 Harbor 的调度/沙箱/验证逻辑，也不做重试决策（由上层 retry budget 决定）。
    """

    machines: dict[str, TrialMachine] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def machine_for(self, trial_id: str) -> TrialMachine:
        """取/建 trial 状态机。"""
        if trial_id not in self.machines:
            self.machines[trial_id] = TrialMachine(trial_id)
        return self.machines[trial_id]

    def apply_event(self, envelope: Mapping[str, Any]) -> TrialState | None:
        """应用一个事件信封（docs/02 §11 的 JSONL 行）。

        Returns:
            新状态；无法映射/事件不需要改状态时返回 None。

        Raises:
            IllegalTransition: Harbor 事件顺序与 docs/02 §4.2 转移表冲突
                （说明上游状态机不一致，必须立即暴露而不是吞掉）。
        """
        trial_id = str(envelope.get("trial_id", ""))
        event_type = str(envelope.get("type", ""))
        if not trial_id or not event_type:
            raise ValueError(f"事件信封缺 trial_id/type: {envelope!r}")
        machine = self.machine_for(trial_id)
        mapped = event_to_machine_event(event_type, envelope.get("payload"))
        if mapped is None:
            return None
        if mapped == "invalidate_post_hoc" and machine.state != TrialState.SCORED:
            mapped = "invalidate"
        return machine.apply(mapped, note=event_type)

    def apply_events(self, envelopes: Iterable[Mapping[str, Any]]) -> None:
        """批量应用事件流。"""
        for envelope in envelopes:
            self.apply_event(envelope)

    def record_error(
        self,
        trial_id: str,
        *,
        cause: str,
        message: str = "",
        error_type: str = "",
    ) -> TrialError:
        """记录一次 Harbor 错误并落到对应 trial 的错误列表。"""
        error = error_from_harbor(cause, message=message, error_type=error_type)
        machine = self.machine_for(trial_id)
        machine.record.errors.append(error)
        self.errors.append({"trial_id": trial_id, **error.to_dict()})
        return error

    def snapshot(self) -> dict[str, Any]:
        """导出全部 trial 状态（供 Result Store 落库）。"""
        return {trial_id: machine.snapshot() for trial_id, machine in sorted(self.machines.items())}
