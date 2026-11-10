"""``batch.yaml``: batch definition (TECH-DESIGN section 14.3)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._yaml import load_yaml_mapping
from .errors import ConfigError

__all__ = [
    "AgentJudgeConfig",
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


class AgentJudgeConfig(BaseModel):
    """``agent_judge``: judge agents for ``agent_judge`` rubric items.

    Present means configured; absent keeps such items inconclusive for a human.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Judge model slugs serving agent items; empty means the batch's ``judges``.
    judges: list[str] = Field(default_factory=list)
    #: Samples per item and judge; each sample boots one judge sandbox.
    k: int = Field(default=3, ge=1, le=9)
    #: Executed actions per sample; replies without a valid first action do not count.
    max_steps: int = Field(default=12, ge=1, le=64)
    #: Invalid replies tolerated per sample; one more ends it as inconclusive ("protocol").
    max_invalid_replies: int = Field(default=4, ge=0, le=32)
    command_timeout_s: float = Field(default=60.0, gt=0, le=600)
    wall_clock_s: float = Field(default=300.0, gt=0, le=3600)
    output_cap_bytes: int = Field(default=8192, ge=1024, le=1 << 20)
    #: False runs judge agents on the final verification only (cost: k x items x boots).
    run_every_round: bool = False

    @field_validator("judges")
    @classmethod
    def _judges_nonempty(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("entries must not be empty strings")
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
    #: Appendix C.5 wants judges from at least two vendors; a single-vendor panel is an
    #: explicit, recorded exception (for example a local run against one fake judge).
    allow_single_judge_vendor: bool = False
    agent_judge: AgentJudgeConfig | None = None
    pricing_table: str

    @field_validator("batch")
    @classmethod
    def _batch_id(cls, value: str) -> str:
        if not BATCH_ID_RE.match(value):
            raise ValueError(f"batch id must look like 'YYYY-MM-bN' (PRD A.1): {value!r}")
        return value

    @model_validator(mode="after")
    def _agent_judges(self) -> BatchFileConfig:
        if self.agent_judge is not None and not (self.agent_judge.judges or self.judges):
            raise ValueError("agent_judge needs judges: set agent_judge.judges or judges")
        return self

    @property
    def agent_judge_slugs(self) -> list[str]:
        """The judge models serving agent_judge items (empty when not configured)."""
        if self.agent_judge is None:
            return []
        return self.agent_judge.judges or self.judges

    @property
    def judge_panel(self) -> list[str]:
        """Every judge endpoint the batch serves: the llm_judge panel plus agent_judge judges."""
        return sorted({*self.judges, *self.agent_judge_slugs})

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
