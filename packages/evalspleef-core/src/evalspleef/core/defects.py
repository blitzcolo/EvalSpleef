"""evalspleef.core.defects — 缺陷发现、去重与跨轮跟踪（defect.schema.json 落地）。

缺陷 ID 规则见 docs/00-conventions.md §2：{rubric_item_id}#{首次发现轮次}，
例如 R3#2 表示第 2 轮发现的 R3 缺陷。同一 rubric 条目在同一时刻最多一个
未修复缺陷；修复后复发按新缺陷重新计扣分（grade.defect_summary.regressed）。

状态机（docs/02 §5.4 / defect.schema.json $defs.defect_status）
    NEW   本轮首次发现（出现在当轮报告中）
     | observe() 结束自动转
    OPEN  已登记、等待下一轮复测
     | 下一轮复测失败              | 下一轮复测通过
    PERSISTING 复测仍失败            FIXED 确认修复
     | 再复测通过
    FIXED

progress（defect.schema.json observation_item.progress）：none/partial/fixed。
反馈控制器的“连续无进展”只累计 none（partial 视为有改善）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    Defect,
    DefectProgress,
    DefectStatus,
    Evidence,
    FeedbackLevel,
    ObservationSource,
    RubricItem,
    RubricSet,
    Severity,
    defect_id_for,
)

__all__ = [
    "DefectCatalog",
    "DefectRoundReport",
    "DefectTracker",
    "UnknownRubricItem",
]


class UnknownRubricItem(KeyError):
    """观察到的 rubric item 不在 rubric 集合/缺陷目录中。"""


class DefectCatalog:
    """rubric 条目的运行时目录，供缺陷跟踪与 F1-F3 话术生成使用。"""

    def __init__(self, items: Iterable[RubricItem]) -> None:
        catalog: dict[str, RubricItem] = {}
        for item in items:
            if item.id in catalog:
                raise ValueError(f"rubric item ID 重复: {item.id!r}")
            catalog[item.id] = item
        if not catalog:
            raise ValueError("rubric 目录不能为空")
        self._items = catalog

    @classmethod
    def from_rubric_set(cls, rubric_set: RubricSet) -> DefectCatalog:
        """从 RubricSet 构造目录。"""
        return cls(rubric_set.items)

    @classmethod
    def from_dicts(cls, items: Iterable[Mapping[str, Any]]) -> DefectCatalog:
        """从 rubric item JSON/YAML 条目列表构造（11 字段契约的兼容入口）。"""
        from .models import _rubric_item_from_dict  # 局部 import 避免暴露到 __all__

        return cls(_rubric_item_from_dict(dict(item)) for item in items)

    def get(self, item_id: str) -> RubricItem:
        """按 ID 取 rubric 条目；不存在时抛 UnknownRubricItem。"""
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise UnknownRubricItem(
                f"rubric item {item_id!r} 不在目录中；已知: {sorted(self._items)}"
            ) from exc

    def severity_of(self, item_id: str) -> Severity:
        """取 rubric 条目的严重度。"""
        return self.get(item_id).severity

    def symptom_of(self, item_id: str) -> str:
        """取 F1 现象描述模板（rubric.f1_symptom）。"""
        return self.get(item_id).f1_symptom

    def f2_evidence_of(self, item_id: str) -> str:
        """取 F2 允许引用的日志/报错模板。"""
        return self.get(item_id).f2_evidence

    def f3_visual_of(self, item_id: str) -> str:
        """取 F3 允许的视觉差异描述模板。"""
        return self.get(item_id).f3_visual

    def f3_assist_of(self, item_id: str) -> str:
        """取 F3 允许的协助操作模板。"""
        return self.get(item_id).f3_assist

    def is_hidden(self, item_id: str) -> bool:
        """该条目是否对应隐藏验收（反馈只能用 f1_symptom）。"""
        return self.get(item_id).hidden

    @property
    def items(self) -> tuple[RubricItem, ...]:
        """目录内全部条目（按 ID 升序）。"""
        return tuple(self._items[key] for key in sorted(self._items))

    @property
    def kind(self) -> str:
        """目录类型标记（便于日志）。"""
        return "rubric_catalog"

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._items

    def to_list(self) -> list[dict[str, Any]]:
        """序列化为 rubric 条目 dict 列表（键名与 rubric schema 一致）。"""
        return [item.to_dict() for item in self.items]


@dataclass(frozen=True)
class DefectRoundReport:
    """一轮观察的缺陷变更报告。"""

    round_no: int
    new: tuple[str, ...]
    persisting: tuple[str, ...]
    fixed: tuple[str, ...]
    open: tuple[str, ...]
    partial: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """各状态计数（用于日志与报告）。"""
        return {
            "new": len(self.new),
            "persisting": len(self.persisting),
            "fixed": len(self.fixed),
            "open": len(self.open),
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "round": self.round_no,
            "new": list(self.new),
            "persisting": list(self.persisting),
            "fixed": list(self.fixed),
            "open": list(self.open),
            "partial": list(self.partial),
            "counts": self.counts,
        }


class DefectTracker:
    """跨轮缺陷跟踪器（一轮完整观察输入，输出增量 + 结构化快照）。

    典型用法：
        tracker = DefectTracker(catalog)
        tracker.observe(1, ["R1", "R3"])   # 第 1 轮发现两个缺陷
        tracker.observe(2, ["R3"])         # R1 修复，R3 仍失败
        tracker.active_defects()            # -> (R3#1,)
        tracker.mark_feedback("R3#1", FeedbackLevel.F2)

    轮次必须严格递增；未出现在集合里的已登记缺陷视为本轮复测通过。
    """

    def __init__(self, catalog: DefectCatalog) -> None:
        self.catalog = catalog
        self._defects: dict[str, Defect] = {}
        self._regressed: set[str] = set()
        self._last_round = 0

    # -- 查询 -------------------------------------------------------------

    @property
    def last_round(self) -> int:
        """最近一次观察的轮次；尚未观察时为 0。"""
        return self._last_round

    @property
    def defects(self) -> tuple[Defect, ...]:
        """全部缺陷，按 (首次发现轮次, ID) 升序。"""
        return tuple(sorted(self._defects.values(), key=lambda d: (d.first_round, d.defect_id)))

    def get(self, defect_id: str) -> Defect:
        """按 ID 取缺陷；不存在时抛 KeyError。"""
        return self._defects[defect_id]

    def active_defects(self) -> tuple[Defect, ...]:
        """尚未修复的缺陷（OPEN/PERSISTING/NEW）。"""
        return tuple(
            defect
            for defect in self.defects
            if defect.status in (DefectStatus.NEW, DefectStatus.OPEN, DefectStatus.PERSISTING)
        )

    #: 兼容别名：反馈控制器语义上关心“还没修好的”。
    open_defects = active_defects

    def blocking_open(self) -> tuple[Defect, ...]:
        """未修复且 severity ∈ {functional, fatal} 的缺陷（阻断成功条件）。"""
        return tuple(defect for defect in self.active_defects() if defect.is_blocking)

    def fixed_defects(self) -> tuple[Defect, ...]:
        """已确认修复的缺陷。"""
        return tuple(defect for defect in self.defects if defect.status == DefectStatus.FIXED)

    def defects_for_item(self, rubric_item_id: str) -> tuple[Defect, ...]:
        """某 rubric 条目对应的全部缺陷（含历史已修复的）。"""
        return tuple(defect for defect in self.defects if defect.rubric_item_id == rubric_item_id)

    def regressed_defects(self) -> tuple[Defect, ...]:
        """修复后复发、按新缺陷登记的缺陷。"""
        return tuple(defect for defect in self.defects if defect.defect_id in self._regressed)

    def status_counts(self) -> dict[str, int]:
        """按状态统计缺陷数。"""
        counts = {str(status): 0 for status in DefectStatus}
        for defect in self._defects.values():
            counts[str(defect.status)] += 1
        return counts

    # -- 观察/更新 --------------------------------------------------------

    def observe(
        self,
        round_no: int,
        failing_item_ids: Iterable[str],
        *,
        evidence: Mapping[str, Sequence[str | Evidence | dict[str, Any]]] | None = None,
        partial_item_ids: Iterable[str] = (),
    ) -> DefectRoundReport:
        """登记一轮观察结果并返回增量报告。

        Args:
            round_no: 轮次，从 1 开始且必须严格大于上一轮。
            failing_item_ids: 该轮观察到的全部失败 rubric item ID；
                未出现的已登记缺陷视为本轮复测通过。
            evidence: 可选，item_id -> 证据列表（字符串按 other/uri 处理）。
            partial_item_ids: 有改善但未通过的 item（progress=partial）。

        Raises:
            ValueError: 轮次不递增。
            UnknownRubricItem: 出现目录外的 rubric item。
        """
        if round_no < 1 or round_no > 99:
            raise ValueError(f"轮次范围 1..99，收到: {round_no!r}")
        if round_no <= self._last_round:
            raise ValueError(f"观察轮次必须严格递增：上一轮 {self._last_round}，收到 {round_no}")
        failing: list[str] = []
        seen: set[str] = set()
        for item_id in failing_item_ids:
            item_id = str(item_id)
            if item_id not in self.catalog:
                raise UnknownRubricItem(f"观察结果包含目录外 rubric item: {item_id!r}")
            if item_id not in seen:
                seen.add(item_id)
                failing.append(item_id)
        partial = {str(item) for item in partial_item_ids}
        unknown_partial = partial - set(self.catalog._items)
        if unknown_partial:
            raise UnknownRubricItem(
                f"partial 结果包含目录外 rubric item: {sorted(unknown_partial)}"
            )
        evidence = evidence or {}

        self._promote_new_to_open()
        fixed_ids: list[str] = []
        persisting_ids: list[str] = []
        partial_ids: list[str] = []
        new_ids: list[str] = []

        active_by_item: dict[str, Defect] = {}
        for defect in self.active_defects():
            active_by_item.setdefault(defect.rubric_item_id, defect)

        for item_id in failing:
            active = active_by_item.get(item_id)
            if active is None:
                evidence_items = tuple(
                    Evidence.from_any(item) for item in evidence.get(item_id, ())
                )
                if any(
                    defect.status == DefectStatus.FIXED for defect in self.defects_for_item(item_id)
                ):
                    regression = True
                else:
                    regression = False
                defect = Defect(
                    defect_id=defect_id_for(item_id, round_no),
                    rubric_item_id=item_id,
                    severity=self.catalog.severity_of(item_id),
                    status=DefectStatus.NEW,
                    first_round=round_no,
                    last_round=round_no,
                    rounds_failed=(round_no,),
                    fixed_round=None,
                    progress=DefectProgress.NONE,
                    evidence=evidence_items,
                )
                self._defects[defect.defect_id] = defect
                if regression:
                    self._regressed.add(defect.defect_id)
                new_ids.append(defect.defect_id)
            else:
                active.status = DefectStatus.PERSISTING
                active.last_round = round_no
                if round_no not in active.rounds_failed:
                    active.rounds_failed = (*active.rounds_failed, round_no)
                active.progress = (
                    DefectProgress.PARTIAL if item_id in partial else DefectProgress.NONE
                )
                active.evidence = self._merge_evidence(active.evidence, evidence.get(item_id, ()))
                persisting_ids.append(active.defect_id)
                if item_id in partial:
                    partial_ids.append(active.defect_id)

        for item_id in partial:
            if item_id in seen:
                continue
            defect = active_by_item.get(item_id)
            if defect is not None and defect.defect_id not in persisting_ids:
                # 有改善但未通过：仍算 persisting（未修复），progress=partial。
                defect.status = DefectStatus.PERSISTING
                defect.last_round = round_no
                if round_no not in defect.rounds_failed:
                    defect.rounds_failed = (*defect.rounds_failed, round_no)
                defect.progress = DefectProgress.PARTIAL
                defect.evidence = self._merge_evidence(defect.evidence, evidence.get(item_id, ()))
                persisting_ids.append(defect.defect_id)
                partial_ids.append(defect.defect_id)

        # 未失败的活跃缺陷 => 本轮复测通过。
        for item_id, defect in active_by_item.items():
            if item_id in seen or item_id in partial:
                continue
            defect.status = DefectStatus.FIXED
            defect.fixed_round = round_no
            defect.last_round = round_no
            defect.progress = DefectProgress.FIXED
            fixed_ids.append(defect.defect_id)

        self._last_round = round_no
        # 本轮新登记缺陷进入 OPEN，等待下一轮复测。
        for defect_id in new_ids:
            self._defects[defect_id].status = DefectStatus.OPEN

        open_ids = tuple(defect.defect_id for defect in self.active_defects())
        return DefectRoundReport(
            round_no=round_no,
            new=tuple(new_ids),
            persisting=tuple(persisting_ids),
            fixed=tuple(fixed_ids),
            open=open_ids,
            partial=tuple(partial_ids),
        )

    def _merge_evidence(
        self,
        existing: tuple[Evidence, ...],
        incoming: Sequence[str | Evidence | dict[str, Any]],
    ) -> tuple[Evidence, ...]:
        """合并证据（按 kind+uri 去重）。"""
        merged = list(existing)
        keys = {(item.kind.value, item.uri) for item in merged}
        for raw in incoming:
            item = Evidence.from_any(raw)
            key = (item.kind.value, item.uri)
            if key not in keys:
                keys.add(key)
                merged.append(item)
        return tuple(merged)

    def _promote_new_to_open(self) -> None:
        """把上一轮残留的 NEW 状态提升为 OPEN（防御性，正常流程不会触发）。"""
        for defect in self._defects.values():
            if defect.status == DefectStatus.NEW:
                defect.status = DefectStatus.OPEN

    def mark_feedback(self, defect_id: str, level: FeedbackLevel) -> Defect:
        """记录为某缺陷发了一轮反馈（更新 feedback_rounds 与最高级别）。"""
        defect = self._defects[defect_id]
        if not defect.is_open:
            raise ValueError(f"缺陷 {defect_id} 已修复，不能再记反馈")
        defect.feedback_rounds += 1
        current = defect.max_level_during_open
        order = ("F1", "F2", "F3")
        if str(level) != "F0" and (
            current is None or order.index(str(level)) > order.index(str(current))
        ):
            defect.max_level_during_open = level
        return defect

    # -- 输出 -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """本轮缺陷汇总（defect.schema.json $defs.summary）。"""
        by_severity = {str(severity): 0 for severity in Severity}
        for defect in self._defects.values():
            by_severity[str(defect.severity)] += 1
        requiring_feedback = sum(
            1 for defect in self._defects.values() if defect.feedback_rounds > 0
        )
        return {
            "new": 0,
            "persisting": 0,
            "fixed": 0,
            "open": len(self.active_defects()),
            "blocking_open": len(self.blocking_open()),
            "total_discovered": len(self._defects),
            "defects_requiring_feedback": requiring_feedback,
            "by_severity": by_severity,
        }

    def summary_after(self, report: DefectRoundReport) -> dict[str, Any]:
        """带本轮增量计数的汇总（用于 observation_document）。"""
        payload = self.summary()
        payload.update(
            {
                "new": len(report.new),
                "persisting": len(report.persisting),
                "fixed": len(report.fixed),
                "open": len(report.open),
            }
        )
        return payload

    def observation_document(
        self,
        trial_id: str,
        *,
        round_no: int,
        observed_at: str,
        source: ObservationSource | str = ObservationSource.MIXED,
        report: DefectRoundReport | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """生成 defect.schema.json 的 observation_document。"""
        if report is None:
            report = DefectRoundReport(
                round_no=round_no,
                new=(),
                persisting=(),
                fixed=(),
                open=tuple(defect.defect_id for defect in self.active_defects()),
            )
        document: dict[str, Any] = {
            "document_type": "observation",
            "schema_version": "1.0",
            "trial_id": trial_id,
            "round": round_no,
            "observed_at": observed_at,
            "source": str(source),
            "items": [defect.to_dict() for defect in self.defects],
            "summary": self.summary_after(report),
        }
        if extensions:
            document["extensions"] = dict(extensions)
        return document

    def to_dict(self) -> dict[str, Any]:
        """缺陷清单快照（items 为 observation_item，含内部跨轮字段）。"""
        return {
            "rounds_observed": self._last_round,
            "items": [defect.to_dict() for defect in self.defects],
            "summary": self.summary(),
        }
