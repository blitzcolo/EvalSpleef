"""evalspleef.core.report — trial -> 榜单单元格聚合与批次报告。

事实来源：
- docs/00-conventions.md §4.3/§4.4：档位展示、数值映射、单元格格式
  {扣分}/{档位}({费用,2 位小数})，如 6/A(290.08)；
- docs/02-eval-flow.md §2/§7.3/§10：cell 定义、3 个 epoch 取中位档位、
  展示最好/最差、档位跨度 ≥2 进人工复核、费用取 median(cost.agent)、
  Pass 继承与 Skip 的人工状态；Cell 状态 Pending/Running/Scored/Pass/Skip/Manual；
- drafts/plan-v1.md §4.4：3 次独立 trial，报中位档位，悬停显示最好最差。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .cost import CostBasis
from .models import (
    CELL_FORMAT,
    CellStatus,
    GradeStatus,
    Tier,
    TrialState,
    Validity,
    display_tier,
    median_tier,
    parse_batch_id,
    parse_trial_id,
    tier_value,
)

__all__ = [
    "BatchReport",
    "Cell",
    "TrialResult",
    "aggregate_cell",
    "cell_id_for",
    "format_cell",
    "median_low",
]

#: 档位跨度达到该值即进人工复核队列（docs/02 §10：跨度 ≥2 档）。
TIER_SPREAD_MANUAL_REVIEW = 2


def median_low(values: Sequence[int | float]) -> int | float:
    """较低中位数：偶数个取较差一侧（偏保守，避免高估模型）。"""
    if not values:
        raise ValueError("无法对空序列求中位数")
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def cell_id_for(task_id: str, model: str, shell: str) -> str:
    """cell ID：{task_id}.{model}.{shell}（docs/02 §2）。"""
    return f"{task_id}.{model}.{shell}"


@dataclass(frozen=True)
class TrialResult:
    """一个 trial 出分后的聚合视图（从 trial 记录 JSON 解析）。"""

    trial_id: str
    task_id: str
    model: str
    shell: str
    epoch: int
    index: int
    validity: Validity
    state: TrialState
    tier: Tier | None = None
    penalty: int | None = None
    cost_agent: float | None = None
    grade_status: GradeStatus | None = None
    counted_in_leaderboard: bool = False
    cheat: bool = False
    format_violation: bool = False
    grade: dict[str, Any] | None = None
    cost_basis: CostBasis | None = None
    provenance: Mapping[str, Any] | None = None

    @property
    def cell_id(self) -> str:
        """该 trial 所属单元格 ID。"""
        return cell_id_for(self.task_id, self.model, self.shell)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TrialResult:
        """从 trial 记录 JSON 解析（grade 取 grades 最后一个版本）。"""
        trial_id = str(record["trial_id"])
        parts = parse_trial_id(trial_id)
        validity = Validity(str(record.get("validity", "pending")))
        state = TrialState(str(record.get("state", "pending")))
        grades = list(record.get("grades") or ())
        grade = grades[-1] if grades else None
        tier: Tier | None = None
        penalty: int | None = None
        cost_agent: float | None = None
        grade_status: GradeStatus | None = None
        counted = False
        cheat = False
        format_violation = False
        cost_basis: CostBasis | None = None
        if grade is not None:
            tier = Tier(str(grade["tier"]))
            penalty = int(grade.get("penalty", 0))
            cost = grade.get("cost") or {}
            agent = cost.get("agent")
            cost_agent = float(agent) if agent is not None else None
            grade_status = GradeStatus(str(grade.get("status", "final")))
            counted = bool(grade.get("counted_in_leaderboard", False))
            flags = {str(flag) for flag in grade.get("flags", ())}
            cheat = "cheat" in flags
            format_violation = "format_violation" in flags
            raw_cost = grade.get("cost")
            raw_basis = record.get("cost_basis")
            if raw_basis is None and isinstance(raw_cost, Mapping):
                raw_basis = raw_cost.get("basis")
            cost_basis = None if raw_basis is None else CostBasis(str(raw_basis))
        raw_provenance = record.get("provenance")
        provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else None
        return cls(
            trial_id=trial_id,
            task_id=parts.task_id,
            model=parts.model,
            shell=parts.shell,
            epoch=parts.epoch,
            index=parts.index,
            validity=validity,
            state=state,
            tier=tier,
            penalty=penalty,
            cost_agent=cost_agent,
            grade_status=grade_status,
            counted_in_leaderboard=counted,
            cheat=cheat,
            format_violation=format_violation,
            grade=dict(grade) if grade is not None else None,
            cost_basis=cost_basis,
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "trial_id": self.trial_id,
            "task_id": self.task_id,
            "model": self.model,
            "shell": self.shell,
            "epoch": self.epoch,
            "index": self.index,
            "validity": str(self.validity),
            "state": str(self.state),
            "tier": None if self.tier is None else str(self.tier),
            "penalty": self.penalty,
            "cost_agent": self.cost_agent,
            "grade_status": None if self.grade_status is None else str(self.grade_status),
            "counted_in_leaderboard": self.counted_in_leaderboard,
            "cheat": self.cheat,
            "format_violation": self.format_violation,
        }
        if self.cost_basis is not None:
            payload["cost_basis"] = str(self.cost_basis)
        if self.provenance:
            payload["provenance"] = dict(self.provenance)
        return payload


@dataclass
class Cell:
    """榜单单元格：模型 × 题目（跨 epoch 聚合）。"""

    task_id: str
    model: str
    shell: str
    status: CellStatus = CellStatus.PENDING
    tier: Tier | None = None
    penalty: int | None = None
    cost_agent: float | None = None
    best_tier: Tier | None = None
    worst_tier: Tier | None = None
    trial_count: int = 0
    inherit_from: str | None = None
    skip_reason: str | None = None
    manual_reasons: tuple[str, ...] = ()
    cheat: bool = False
    format_violation: bool = False
    extensions: dict[str, Any] = field(default_factory=dict)
    cost_basis: CostBasis | None = None
    provenance: Mapping[str, Any] | None = None

    @property
    def cell_id(self) -> str:
        """{task_id}.{model}.{shell}。"""
        return cell_id_for(self.task_id, self.model, self.shell)

    @property
    def display_label(self) -> str:
        """榜单展示标签：模型名；Pass 时附带继承来源。"""
        if self.inherit_from:
            return f"{self.model}（继承自 {self.inherit_from}）"
        return self.model

    def format(self) -> str:
        """渲染单元格：扣分/档位(费用,2 位小数)（00-conventions §4.4 / docs/03 §10.4）。

        - 正常：6/A(290.08)
        - 作弊：0/Failed*(12.50)（* = cheat 标记）
        - 格式违规：0/Failed†(12.50)（† = format_violation）
        - Pass 继承：A(继承自 gpt-5-codex)
        """
        if self.status == CellStatus.PASS and self.tier is not None:
            return f"{display_tier(self.tier)}(继承自 {self.inherit_from})"
        if self.status == CellStatus.SCORED and self.tier is not None:
            base = CELL_FORMAT.format(
                penalty=int(self.penalty or 0),
                tier=display_tier(self.tier),
                cost=float(self.cost_agent or 0.0),
            )
            if self.cheat:
                base = base.replace(
                    f"/{display_tier(self.tier)}", f"/{display_tier(self.tier)}*", 1
                )
            elif self.format_violation:
                base = base.replace(
                    f"/{display_tier(self.tier)}", f"/{display_tier(self.tier)}†", 1
                )
            return base
        if self.status == CellStatus.SKIP:
            return f"Skip（{self.skip_reason}）" if self.skip_reason else "Skip"
        if self.status == CellStatus.MANUAL:
            return "Manual"
        if self.status == CellStatus.RUNNING:
            return "Running"
        return "Pending"

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        payload: dict[str, Any] = {
            "cell_id": self.cell_id,
            "task_id": self.task_id,
            "model": self.model,
            "shell": self.shell,
            "status": str(self.status),
            "cell": self.format(),
            "display_label": self.display_label,
            "tier": None if self.tier is None else str(self.tier),
            "tier_display": None if self.tier is None else display_tier(self.tier),
            "tier_numeric": None if self.tier is None else tier_value(self.tier),
            "penalty": self.penalty,
            "cost_agent": self.cost_agent,
            "best_tier": None if self.best_tier is None else str(self.best_tier),
            "worst_tier": None if self.worst_tier is None else str(self.worst_tier),
            "trial_count": self.trial_count,
            "inherit_from": self.inherit_from,
            "skip_reason": self.skip_reason,
            "manual_reasons": list(self.manual_reasons),
            "cheat": self.cheat,
            "format_violation": self.format_violation,
            "manual_review": bool(self.manual_reasons),
        }
        if self.extensions:
            payload["extensions"] = dict(self.extensions)
        if self.cost_basis is not None:
            payload["cost_basis"] = str(self.cost_basis)
        if self.provenance:
            payload["provenance"] = dict(self.provenance)
        return payload


def _cell_group_key(trial: TrialResult) -> tuple[str, str, str]:
    """按 (task, model, shell) 分组。"""
    return trial.task_id, trial.model, trial.shell


def _median_penalty(penalties: Sequence[int]) -> int:
    """扣分取较低中位数（整数）。"""
    return int(median_low(penalties))


def aggregate_cell(
    trials: Sequence[TrialResult],
    *,
    inherit_from: Cell | None = None,
    skip_reason: str | None = None,
) -> Cell:
    """把一个 cell 下的 trial 聚合为榜单单元格。

    聚合规则（docs/02 §10）：
    - 只统计 validity=valid、grade.status=final 且 counted_in_leaderboard=true 的 trial；
    - 档位取中位，最好/最差保留展示；跨度 ≥2 档进人工复核；
    - 扣分取较低中位数；费用取 median(cost.agent)（docs/02 §7.3）；
    - Pass：继承前代模型成绩并注明来源；Skip：人工标注原因。

    Raises:
        ValueError: trials 不属于同一个 cell。
    """
    if not trials:
        raise ValueError("聚合 cell 需要至少一个 trial")
    keys = {_cell_group_key(trial) for trial in trials}
    if len(keys) != 1:
        raise ValueError(f"同一 cell 的 trial 必须同 task/model/shell，收到: {sorted(keys)}")
    task_id, model, shell = next(iter(keys))

    if skip_reason is not None:
        return Cell(
            task_id=task_id,
            model=model,
            shell=shell,
            status=CellStatus.SKIP,
            skip_reason=skip_reason,
            trial_count=len(trials),
        )
    if inherit_from is not None:
        return Cell(
            task_id=task_id,
            model=model,
            shell=shell,
            status=CellStatus.PASS,
            tier=inherit_from.tier,
            penalty=inherit_from.penalty,
            cost_agent=0.0,
            best_tier=inherit_from.tier,
            worst_tier=inherit_from.tier,
            trial_count=0,
            inherit_from=inherit_from.model,
            extensions={"inherited_cell": inherit_from.cell_id},
        )

    counted = [
        trial
        for trial in trials
        if trial.validity == Validity.VALID
        and trial.counted_in_leaderboard
        and trial.tier is not None
    ]
    manual_trials = [
        trial
        for trial in trials
        if trial.validity == Validity.MANUAL or trial.grade_status == GradeStatus.PROVISIONAL
    ]
    if not counted:
        if manual_trials:
            reasons = tuple(
                sorted(
                    {
                        reason
                        for trial in manual_trials
                        for reason in ((trial.grade or {}).get("manual_notes") or "").split("；")
                        if reason
                    }
                )
            )
            return Cell(
                task_id=task_id,
                model=model,
                shell=shell,
                status=CellStatus.MANUAL,
                trial_count=len(trials),
                manual_reasons=reasons or ("存在待人工确认的 trial",),
            )
        if any(trial.state == TrialState.RUNNING for trial in trials):
            return Cell(
                task_id=task_id,
                model=model,
                shell=shell,
                status=CellStatus.RUNNING,
                trial_count=len(trials),
            )
        return Cell(
            task_id=task_id,
            model=model,
            shell=shell,
            status=CellStatus.PENDING,
            trial_count=len(trials),
        )

    if len(counted) < 2:
        return Cell(
            task_id=task_id,
            model=model,
            shell=shell,
            status=CellStatus.MANUAL,
            trial_count=len(counted),
            manual_reasons=(
                f"有效 epoch 仅 {len(counted)} 个（<2），cell 转 Manual 不进榜（docs/03 §9.9）",
            ),
        )
    tiers = [trial.tier for trial in counted if trial.tier is not None]
    assert all(tier is not None for tier in tiers)
    tier_values = [tier_value(tier) for tier in tiers if tier is not None]
    median = median_tier(tiers)
    best = max(tiers, key=tier_value)
    worst = min(tiers, key=tier_value)
    penalties = [int(trial.penalty or 0) for trial in counted]
    costs = [float(trial.cost_agent or 0.0) for trial in counted]
    spread = max(tier_values) - min(tier_values)
    manual_reasons: list[str] = []
    if spread >= TIER_SPREAD_MANUAL_REVIEW:
        manual_reasons.append(
            f"档位跨度 {spread} 档（最好 {best} / 最差 {worst}）≥ {TIER_SPREAD_MANUAL_REVIEW}，进人工复核"
        )
    for trial in counted:
        if trial.validity == Validity.MANUAL:
            manual_reasons.append(f"{trial.trial_id} 进入人工复核")
    bases = {trial.cost_basis for trial in counted if trial.cost_basis is not None}
    provenances = [trial.provenance for trial in counted if trial.provenance]
    provenance = (
        dict(provenances[0])
        if provenances and all(item == provenances[0] for item in provenances)
        else None
    )
    return Cell(
        task_id=task_id,
        model=model,
        shell=shell,
        status=CellStatus.SCORED,
        tier=median,
        penalty=_median_penalty(penalties),
        cost_agent=float(median_low(costs)),
        best_tier=best,
        worst_tier=worst,
        trial_count=len(counted),
        manual_reasons=tuple(manual_reasons),
        cheat=any(trial.cheat for trial in counted),
        format_violation=any(trial.format_violation for trial in counted),
        cost_basis=next(iter(bases)) if len(bases) == 1 else None,
        provenance=provenance,
        extensions={
            "epoch_tiers": [str(trial.tier) for trial in counted],
            "epoch_penalties": penalties,
            "epoch_costs": [round(cost, 6) for cost in costs],
        },
    )


def format_cell(cell: Cell) -> str:
    """便捷函数：渲染单元格文本。"""
    return cell.format()


@dataclass
class BatchReport:
    """一个批次的榜单报告（markdown + JSON）。"""

    batch_id: str
    trials: tuple[TrialResult, ...]
    inherited: Mapping[str, Cell] = field(default_factory=dict)
    skips: Mapping[str, str] = field(default_factory=dict)
    generated_at: str | None = None

    def __post_init__(self) -> None:
        parse_batch_id(self.batch_id)  # 非法批次 ID 直接报错
        self.trials = tuple(self.trials)

    def _groups(self) -> dict[tuple[str, str, str], list[TrialResult]]:
        """按 cell 分组 trial。"""
        groups: dict[tuple[str, str, str], list[TrialResult]] = {}
        for trial in self.trials:
            groups.setdefault(_cell_group_key(trial), []).append(trial)
        return groups

    def build_cells(self) -> tuple[Cell, ...]:
        """生成全部单元格（按 task_id / model / shell 排序）。"""
        cells: list[Cell] = []
        for key in sorted(self._groups()):
            trials = self._groups()[key]
            cell_id = cell_id_for(*key)
            inherit = self.inherited.get(cell_id)
            skip_reason = self.skips.get(cell_id)
            cells.append(
                aggregate_cell(
                    trials,
                    inherit_from=inherit,
                    skip_reason=skip_reason,
                )
            )
        return tuple(cells)

    @property
    def review_queue(self) -> tuple[Cell, ...]:
        """人工复核队列：档位跨度异常 / Manual / 待确认。"""
        return tuple(
            cell
            for cell in self.build_cells()
            if cell.manual_reasons or cell.status == CellStatus.MANUAL
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        cells = self.build_cells()
        return {
            "batch_id": self.batch_id,
            "generated_at": self.generated_at,
            "trial_count": len(self.trials),
            "cell_count": len(cells),
            "review_queue": [cell.cell_id for cell in self.review_queue],
            "cells": [cell.to_dict() for cell in cells],
        }

    def _table(self) -> list[str]:
        """渲染 task × (model[/shell]) 的 markdown 表格。"""
        cells = self.build_cells()
        tasks = sorted({cell.task_id for cell in cells})
        shells = {cell.shell for cell in cells}
        multi_shell = len(shells) > 1
        columns: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for cell in sorted(cells, key=lambda item: (item.model, item.shell)):
            label = f"{cell.model}/{cell.shell}" if multi_shell else cell.model
            if (cell.model, cell.shell) not in seen:
                seen.add((cell.model, cell.shell))
                columns.append((label, cell.model))
        index: dict[tuple[str, str, str], Cell] = {
            (cell.task_id, cell.model, cell.shell): cell for cell in cells
        }
        lines = [
            "| 题目 | " + " | ".join(label for label, _ in columns) + " |",
            "| --- | " + " | ".join("---" for _ in columns) + " |",
        ]
        for task in tasks:
            row = [task]
            for _label, model in columns:
                matched = [
                    cell
                    for (cell_task, cell_model, _shell), cell in index.items()
                    if cell_task == task and cell_model == model
                ]
                row.append(matched[0].format() if matched else "—")
            lines.append("| " + " | ".join(row) + " |")
        return lines

    def to_markdown(self) -> str:
        """渲染批次报告 markdown（发布用的初稿）。"""
        cells = self.build_cells()
        counts: dict[str, int] = {}
        for cell in cells:
            counts[str(cell.status)] = counts.get(str(cell.status), 0) + 1
        lines = [
            f"# 批次报告 {self.batch_id}",
            "",
            "> 规则：单元格格式 扣分/档位(费用)；费用取 median(cost.agent)；"
            "档位取 epoch 中位数；Pass 继承前代成绩。",
            "",
            f"- trial 数：{len(self.trials)}",
            f"- cell 数：{len(cells)}",
            "- cell 状态："
            + "，".join(f"{name}={count}" for name, count in sorted(counts.items())),
            f"- 人工复核队列：{len(self.review_queue)} 个 cell",
        ]
        if self.generated_at:
            lines.append(f"- 生成时间：{self.generated_at}")
        lines.extend(["", "## 榜单", ""])
        lines.extend(self._table())
        if self.review_queue:
            lines.extend(["", "## 人工复核队列", ""])
            for cell in self.review_queue:
                reasons = "；".join(cell.manual_reasons) or "待人工确认"
                lines.append(f"- {cell.cell_id}：{reasons}")
        return "\n".join(lines)


def load_trial_results(records: Iterable[Mapping[str, Any]]) -> tuple[TrialResult, ...]:
    """批量解析 trial 记录 JSON。"""
    return tuple(TrialResult.from_record(record) for record in records)
