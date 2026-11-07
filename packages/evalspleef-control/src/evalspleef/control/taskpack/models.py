"""Task-pack model: budget fields and the loaded pack."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalspleef.core.defects import DefectCatalog
from evalspleef.core.models import RubricSet
from evalspleef.spi.caps import ExecutionMode, RuntimeRequirements

__all__ = ["Budget", "Escalation", "TaskPack"]


@dataclass(frozen=True)
class Escalation:
    """Feedback escalation thresholds (PRD appendix A.2 §4.8)."""

    f1_no_progress_rounds: int = 2
    f2_no_progress_rounds: int = 2

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Escalation:
        return cls(
            f1_no_progress_rounds=int(data.get("f1_no_progress_rounds", 2)),
            f2_no_progress_rounds=int(data.get("f2_no_progress_rounds", 2)),
        )


@dataclass(frozen=True)
class Budget:
    """Trial budget fields; names and defaults are frozen (PRD appendix A.2 §4.8)."""

    max_rounds: int = 6
    max_feedback_rounds: int = 3
    max_nudges: int = 2
    max_total_tokens: int = 2_000_000
    max_wall_clock_s: int = 3600
    max_cost_usd: float = 15.0
    max_observe_s: int = 600
    escalation: Escalation = field(default_factory=Escalation)
    currency: str = "USD"
    pricing_table_version: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Budget:
        escalation = data.get("escalation")
        return cls(
            max_rounds=int(data.get("max_rounds", 6)),
            max_feedback_rounds=int(data.get("max_feedback_rounds", 3)),
            max_nudges=int(data.get("max_nudges", 2)),
            max_total_tokens=int(data.get("max_total_tokens", 2_000_000)),
            max_wall_clock_s=int(data.get("max_wall_clock_s", 3600)),
            max_cost_usd=float(data.get("max_cost_usd", 15.0)),
            max_observe_s=int(data.get("max_observe_s", 600)),
            escalation=Escalation.from_dict(escalation)
            if isinstance(escalation, Mapping)
            else Escalation(),
            currency=str(data.get("currency", "USD")),
            pricing_table_version=(
                str(data["pricing_table_version"])
                if data.get("pricing_table_version") is not None
                else None
            ),
        )

    @property
    def min_required_lifetime_s(self) -> int:
        """Lower bound FR-TSK-03 imposes on ``runtime.requirements.max_lifetime_s``."""
        return self.max_wall_clock_s + self.max_rounds * self.max_observe_s


@dataclass(frozen=True)
class TaskPack:
    """A loaded task pack: metadata plus the core rubric/defect models."""

    task_id: str
    version: str
    track: str
    root: Path
    budget: Budget
    requirements: RuntimeRequirements
    environment: Mapping[str, str | None]
    execution_modes: tuple[ExecutionMode, ...]
    rubric: RubricSet
    defects: DefectCatalog
    metadata: Mapping[str, Any]

    def file(self, relative: str) -> Path:
        """Resolve a pack-relative file path."""
        return self.root / relative
