"""Cell admission: probe results x task requirements (TECH-DESIGN section 6.4, PRD US-04).

``spi.admit_model`` decides which capabilities fall short. A capability that no probe could
determine is never guessed: the cell becomes ``Manual`` instead of a possibly wrong Skip.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from evalspleef.spi.caps import ModelRequirements
from evalspleef.spi.matching import admit_model
from evalspleef.spi.probe import ProbeCheck, ProbeReport

__all__ = ["AdmissionResult", "CellDecision", "CellOutcome", "admit_cells"]

#: Probes whose result decides each admission-relevant capability field.
FIELD_PROBES: Mapping[str, tuple[str, ...]] = {
    "tool_calling": ("P-03",),
    "context_tokens_effective": ("P-09",),
}


class CellOutcome(StrEnum):
    RUN = "run"
    SKIP = "skip"
    MANUAL = "manual"


@dataclass(frozen=True)
class CellDecision:
    model: str
    task: str
    outcome: CellOutcome
    reasons: tuple[str, ...] = ()
    """``capability:<field>`` for a Skip, ``undetermined:<field>`` for Manual."""


@dataclass(frozen=True)
class AdmissionResult:
    decisions: tuple[CellDecision, ...]

    def _by(self, outcome: CellOutcome) -> tuple[CellDecision, ...]:
        return tuple(d for d in self.decisions if d.outcome is outcome)

    @property
    def runnable(self) -> tuple[CellDecision, ...]:
        return self._by(CellOutcome.RUN)

    @property
    def skipped(self) -> tuple[CellDecision, ...]:
        return self._by(CellOutcome.SKIP)

    @property
    def manual(self) -> tuple[CellDecision, ...]:
        return self._by(CellOutcome.MANUAL)


def _determined(check: ProbeCheck | None) -> bool:
    """A probe result counts only if the probe ran to a verdict (not skipped, not an error)."""
    return check is not None and not check.data.get("skipped") and not check.data.get("error")


def _determined_fields(report: ProbeReport | None) -> frozenset[str]:
    if report is None:
        return frozenset()
    checks = {check.probe_id: check for check in report.checks}
    return frozenset(
        field
        for field, probe_ids in FIELD_PROBES.items()
        if all(_determined(checks.get(probe_id)) for probe_id in probe_ids)
    )


def _relevant_fields(req: ModelRequirements) -> list[str]:
    fields: list[str] = []
    if req.tool_calling == "required":
        fields.append("tool_calling")
    if req.min_context_tokens > 0:
        fields.append("context_tokens_effective")
    return fields


def admit_cells(
    models: Mapping[str, ProbeReport | None], tasks: Mapping[str, ModelRequirements]
) -> AdmissionResult:
    """Decide every model x task cell; ``None`` means the model has no probe report yet."""
    decisions: list[CellDecision] = []
    for model, report in models.items():
        determined = _determined_fields(report)
        for task, req in tasks.items():
            undetermined = [f for f in _relevant_fields(req) if f not in determined]
            if report is None:
                reasons = [f"undetermined:{field}" for field in undetermined]
                outcome = CellOutcome.MANUAL if reasons else CellOutcome.RUN
                decisions.append(CellDecision(model, task, outcome, tuple(reasons)))
                continue
            shortfalls = [
                reason
                for reason in admit_model(req, report.capabilities)
                if reason.partition(":")[2] not in undetermined
            ]
            if shortfalls:
                decisions.append(CellDecision(model, task, CellOutcome.SKIP, tuple(shortfalls)))
            elif undetermined:
                decisions.append(
                    CellDecision(
                        model,
                        task,
                        CellOutcome.MANUAL,
                        tuple(f"undetermined:{field}" for field in undetermined),
                    )
                )
            else:
                decisions.append(CellDecision(model, task, CellOutcome.RUN))
    return AdmissionResult(tuple(decisions))
