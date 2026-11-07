"""Batch planner: cells from models x tasks x epochs, skip reasons and the budget plan (docs/02 5.1).

A cell is one model (with its shell) on one task. At freeze each cell is matched statically
against the provider routes and the model's probed capabilities; a cell nothing can host becomes
``Skip(capability:<reason>)`` and gets no trials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evalspleef.core.models import TrialId, format_trial_id
from evalspleef.core.report import cell_id_for
from evalspleef.spi.caps import RuntimeRequirements
from evalspleef.spi.matching import admit_model
from evalspleef.spi.probe import ModelCapabilities
from evalspleef.spi.sandbox import TemplateRef

from ..scheduling import CellMatch, StaticMatcher
from .executor import PlannedTrial

__all__ = [
    "BatchPlan",
    "BatchPlanner",
    "BudgetPlan",
    "ModelPlanInput",
    "PlannedCell",
    "TaskPlanInput",
]


@dataclass(frozen=True)
class ModelPlanInput:
    """A model entry of the batch, resolved to its endpoint and shell."""

    slug: str
    spec_version: int
    shell: str
    endpoint_id: str
    probe_report_id: str | None = None
    revision: str | None = None
    """Frozen weights revision (HF commit sha); ``None`` for hosted API models."""
    self_deployed: bool = False
    capabilities: ModelCapabilities | None = None
    """From the probe report; ``None`` skips the model-capability admission check."""


@dataclass(frozen=True)
class TaskPlanInput:
    """A task version as the planner needs it."""

    task_id: str
    version: str
    requirements: RuntimeRequirements
    template: TemplateRef
    max_cost_usd: float
    """Agent-side cost cap of one trial (task budget)."""


@dataclass(frozen=True)
class PlannedCell:
    cell_id: str
    task: TaskPlanInput
    model: ModelPlanInput
    match: CellMatch
    skip_reasons: tuple[str, ...]
    trials: tuple[PlannedTrial, ...]

    @property
    def skipped(self) -> bool:
        return bool(self.skip_reasons)

    @property
    def skip_reason(self) -> str | None:
        """``capability:<reason>`` entries joined with commas; ``None`` for a runnable cell."""
        return ",".join(self.skip_reasons) if self.skip_reasons else None


@dataclass(frozen=True)
class BudgetPlan:
    """Worst-case agent spend: per-trial cost cap times epochs, summed over runnable cells."""

    per_cell_usd: Mapping[str, float]
    total_usd: float
    cap_usd: float | None

    @property
    def within_cap(self) -> bool:
        return self.cap_usd is None or self.total_usd <= self.cap_usd


@dataclass(frozen=True)
class BatchPlan:
    batch_id: str
    epochs: int
    cells: tuple[PlannedCell, ...]
    budget: BudgetPlan

    @property
    def runnable_cells(self) -> tuple[PlannedCell, ...]:
        return tuple(cell for cell in self.cells if not cell.skipped)

    @property
    def skipped_cells(self) -> tuple[PlannedCell, ...]:
        return tuple(cell for cell in self.cells if cell.skipped)

    @property
    def trials(self) -> tuple[PlannedTrial, ...]:
        return tuple(trial for cell in self.runnable_cells for trial in cell.trials)

    def skips(self) -> dict[str, str]:
        """Cell id -> skip reason, in the shape ``core.report.BatchReport`` expects."""
        return {cell.cell_id: cell.skip_reason or "" for cell in self.skipped_cells}


class BatchPlanner:
    """Builds the cell matrix of a batch."""

    def __init__(self, matcher: StaticMatcher) -> None:
        self._matcher = matcher

    def plan(
        self,
        batch_id: str,
        models: Sequence[ModelPlanInput],
        tasks: Sequence[TaskPlanInput],
        *,
        epochs: int = 1,
        budget_cap_usd: float | None = None,
    ) -> BatchPlan:
        if epochs < 1:
            raise ValueError(f"epochs must be at least 1, got {epochs}")
        cells: list[PlannedCell] = []
        for task in tasks:
            match = self._matcher.match(task.requirements)
            for model in models:
                cells.append(self._cell(batch_id, task, model, match, epochs))
        per_cell = {
            cell.cell_id: cell.task.max_cost_usd * epochs for cell in cells if not cell.skipped
        }
        budget = BudgetPlan(per_cell, sum(per_cell.values()), budget_cap_usd)
        return BatchPlan(batch_id, epochs, tuple(cells), budget)

    @staticmethod
    def _cell(
        batch_id: str, task: TaskPlanInput, model: ModelPlanInput, match: CellMatch, epochs: int
    ) -> PlannedCell:
        cell_id = cell_id_for(task.task_id, model.slug, model.shell)
        reasons = [f"capability:{reason}" for reason in match.reasons]
        if model.capabilities is not None:
            reasons.extend(admit_model(task.requirements.model, model.capabilities))
        trials: tuple[PlannedTrial, ...] = ()
        if not reasons:
            trials = tuple(
                PlannedTrial(
                    trial_id=format_trial_id(
                        TrialId(task.task_id, model.slug, model.shell, epoch, 1)
                    ),
                    batch_id=batch_id,
                    cell_id=cell_id,
                    task_id=task.task_id,
                    model=model.slug,
                    shell=model.shell,
                    epoch=epoch,
                    index=1,
                    requirements=task.requirements,
                    template=task.template,
                    agent_routes=match.agent.candidates,
                    verifier_routes=match.verifier.candidates,
                    endpoint_id=model.endpoint_id,
                )
                for epoch in range(1, epochs + 1)
            )
        return PlannedCell(cell_id, task, model, match, tuple(reasons), trials)
