"""``batch.yaml``: batch definition (TECH-DESIGN section 14.3)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._yaml import load_yaml_mapping
from .errors import ConfigError

__all__ = [
    "BatchBudget",
    "BatchFileConfig",
    "EndpointPolicy",
    "ShellsConfig",
    "TasksConfig",
    "load_batch",
]

#: Batch ids look like ``2026-11-b1`` (PRD appendix A.1).
BATCH_ID_RE = re.compile(r"^\d{4}-\d{2}-b\d+$")


class ShellsConfig(BaseModel):
    """Shell selection: one unified shell, per-model native shell overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unified: str | None = None
    native: dict[str, str] = Field(default_factory=dict)


class TasksConfig(BaseModel):
    """Task selection by glob-like include patterns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    include: list[str] = Field(default_factory=list)


class BatchBudget(BaseModel):
    """Batch-level budget cap; per-task budgets additionally apply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_total_usd: float | None = Field(default=None, gt=0)


class EndpointPolicy(BaseModel):
    """Endpoint lifecycle policy for the batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    warm_before_run: bool = False
    retire_after: str = "sleep"

    @field_validator("retire_after")
    @classmethod
    def _retire_mode(cls, value: str) -> str:
        if value not in ("sleep", "terminate"):
            raise ValueError(f"retire_after must be 'sleep' or 'terminate', got {value!r}")
        return value


class BatchFileConfig(BaseModel):
    """Top level of ``batch.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch: str
    models: list[str] = Field(min_length=1)
    shells: ShellsConfig = Field(default_factory=ShellsConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    epochs: int = Field(default=1, ge=1)
    budget: BatchBudget = Field(default_factory=BatchBudget)
    endpoint_policy: EndpointPolicy = Field(default_factory=EndpointPolicy)
    judges: list[str] = Field(default_factory=list)
    pricing_table: str

    @field_validator("batch")
    @classmethod
    def _batch_id(cls, value: str) -> str:
        if not BATCH_ID_RE.match(value):
            raise ValueError(f"batch id must look like 'YYYY-MM-bN' (PRD A.1): {value!r}")
        return value

    @field_validator("models", "judges")
    @classmethod
    def _names_nonempty(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name.strip():
                raise ValueError("entries must not be empty strings")
        return value


def load_batch(path: str | Path) -> BatchFileConfig:
    """Parse ``batch.yaml``; raises :class:`ConfigError` naming file and path."""
    file = str(path)
    document = load_yaml_mapping(path)
    try:
        return BatchFileConfig.model_validate(document)
    except Exception as exc:
        raise ConfigError.from_validation(file, exc) from exc
