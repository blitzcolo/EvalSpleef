"""The recorded inputs of one ``compute_grade`` call (PRD appendix B.6, NFR-01).

A regrade appends a grade version without re-running the agent, so the inputs
the first grade was computed from must be stored when that grade is made and
read back verbatim. ``GradingInputs`` is that record: it serialises to plain
JSON, parses back to the same values, and ``grade()`` feeds them to the pure
``compute_grade``, so grading the same record twice gives the same grade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from .feedback import FeedbackRound
from .grading import GradeOutcome, HardGate, compute_grade
from .models import (
    Budget,
    BudgetUsage,
    Defect,
    DefectProgress,
    DefectStatus,
    Evidence,
    EvidenceKind,
    FeedbackLevel,
    Severity,
)

__all__ = ["GradingInputs"]


@dataclass(frozen=True)
class GradingInputs:
    """Everything the trial runner passes to ``compute_grade`` besides the clock."""

    trial_id: str
    defects: tuple[Defect, ...]
    hard_gates: tuple[HardGate, ...]
    feedback_rounds: tuple[FeedbackRound, ...] = ()
    budget: Budget | None = None
    usage: BudgetUsage | None = None
    cheat: bool = False
    cheat_evidence: str = ""
    feedback_blocked: bool = False
    cannot_continue: bool = False
    judge_consistency_value: float | None = None
    judge_samples: int = 0
    rubric_version: str = "1.0.0"
    task_version: str = "1.0.0"
    pricing_table_version: str | None = None

    def grade(self, *, decided_at: str) -> GradeOutcome:
        """Grade these inputs; ``decided_at`` is the only value not taken from the record."""
        return compute_grade(
            trial_id=self.trial_id,
            defects=self.defects,
            hard_gates=self.hard_gates,
            feedback_rounds=self.feedback_rounds,
            budget=self.budget,
            usage=self.usage,
            cheat=self.cheat,
            cheat_evidence=self.cheat_evidence,
            feedback_blocked=self.feedback_blocked,
            cannot_continue=self.cannot_continue,
            judge_consistency_value=self.judge_consistency_value,
            judge_samples=self.judge_samples,
            decided_at=decided_at,
            rubric_version=self.rubric_version,
            task_version=self.task_version,
            pricing_table_version=self.pricing_table_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "defects": [_defect_to_dict(defect) for defect in self.defects],
            "hard_gates": [gate.to_dict() for gate in self.hard_gates],
            "feedback_rounds": [item.to_dict() for item in self.feedback_rounds],
            "budget": None if self.budget is None else self.budget.to_dict(),
            "usage": None
            if self.usage is None
            else {f.name: getattr(self.usage, f.name) for f in fields(BudgetUsage)},
            "cheat": self.cheat,
            "cheat_evidence": self.cheat_evidence,
            "feedback_blocked": self.feedback_blocked,
            "cannot_continue": self.cannot_continue,
            "judge_consistency_value": self.judge_consistency_value,
            "judge_samples": self.judge_samples,
            "rubric_version": self.rubric_version,
            "task_version": self.task_version,
            "pricing_table_version": self.pricing_table_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GradingInputs:
        budget = data.get("budget")
        usage = data.get("usage")
        return cls(
            trial_id=str(data["trial_id"]),
            defects=tuple(_defect_from_dict(item) for item in data.get("defects") or ()),
            hard_gates=tuple(
                HardGate(
                    check_id=str(item["check_id"]),
                    passed=bool(item["passed"]),
                    evidence_uri=item.get("evidence_uri"),
                )
                for item in data.get("hard_gates") or ()
            ),
            feedback_rounds=tuple(
                FeedbackRound(
                    round_no=int(item["round"]),
                    level=FeedbackLevel(str(item["level"])),
                    defect_ids=tuple(item.get("defect_ids") or ()),
                    made_progress=item.get("made_progress"),
                    progress=item.get("progress"),
                    text=str(item.get("text") or ""),
                )
                for item in data.get("feedback_rounds") or ()
            ),
            budget=None if budget is None else Budget.from_dict(dict(budget)),
            usage=None if usage is None else BudgetUsage(**dict(usage)),
            cheat=bool(data.get("cheat", False)),
            cheat_evidence=str(data.get("cheat_evidence") or ""),
            feedback_blocked=bool(data.get("feedback_blocked", False)),
            cannot_continue=bool(data.get("cannot_continue", False)),
            judge_consistency_value=data.get("judge_consistency_value"),
            judge_samples=int(data.get("judge_samples") or 0),
            rubric_version=str(data.get("rubric_version") or "1.0.0"),
            task_version=str(data.get("task_version") or "1.0.0"),
            pricing_table_version=data.get("pricing_table_version"),
        )


def _defect_to_dict(defect: Defect) -> dict[str, Any]:
    payload = defect.to_dict()
    # to_dict drops empty notes and a missing judge; keep both so parsing is exact.
    payload["judge"] = None if defect.judge is None else dict(defect.judge)
    payload["note"] = defect.note
    return payload


def _defect_from_dict(data: Mapping[str, Any]) -> Defect:
    level = data.get("max_level_during_open")
    judge = data.get("judge")
    return Defect(
        defect_id=str(data["defect_id"]),
        rubric_item_id=str(data["rubric_item_id"]),
        severity=Severity(str(data["severity"])),
        status=DefectStatus(str(data["status"])),
        first_round=int(data["first_round"]),
        last_round=int(data["last_round"]),
        rounds_failed=tuple(int(n) for n in data.get("rounds_failed") or ()),
        fixed_round=data.get("fixed_round"),
        feedback_rounds=int(data.get("feedback_rounds") or 0),
        max_level_during_open=None if level is None else FeedbackLevel(str(level)),
        progress=DefectProgress(str(data.get("progress") or DefectProgress.NONE)),
        evidence=tuple(
            Evidence(
                kind=EvidenceKind(str(item["kind"])),
                uri=str(item["uri"]),
                sha256=item.get("sha256"),
                excerpt=item.get("excerpt"),
                caption=item.get("caption"),
            )
            for item in data.get("evidence") or ()
        ),
        judge=None if judge is None else dict(judge),
        note=str(data.get("note") or ""),
    )
