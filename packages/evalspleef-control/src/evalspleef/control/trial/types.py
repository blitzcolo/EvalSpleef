"""Value types of one trial run: the context handed to the evaluator and its result."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evalspleef.core.grading import HardGate
from evalspleef.core.models import Budget, EscalationPolicy, Evidence

from ..batch.executor import LeaseHandle, PlannedTrial, TrialLeases
from ..taskpack.models import TaskPack

__all__ = [
    "Artifact",
    "CheatSignal",
    "EvaluationResult",
    "ModelEndpoint",
    "RubricItemResult",
    "RubricVerdict",
    "TrialContext",
    "TrialSetup",
    "WorkspacePaths",
]


@dataclass(frozen=True)
class ModelEndpoint:
    """The model endpoint of the trial, as the gateway exposes it."""

    endpoint_id: str
    model_alias: str
    """Public route name the agent sends to the gateway (``ep_<endpoint_id>``)."""
    gateway_url: str


@dataclass(frozen=True)
class TrialSetup:
    """What the runner needs to know about a planned trial beyond the plan itself."""

    pack: TaskPack
    endpoint: ModelEndpoint


@dataclass(frozen=True)
class WorkspacePaths:
    """Where the workspace lives, on both sides of the sandbox boundary."""

    remote_root: str
    """The workspace path as the agent sees it inside the sandbox."""
    starter_dir: Path | None
    """Pack directory holding the starter files; ``None`` for a pack without starter files."""
    entry_files: tuple[str, ...]
    """Entry files from ``task.yaml`` (``workspace.entry_files``), relative to the workspace."""


@dataclass(frozen=True)
class TrialContext:
    """Everything an evaluator may use for one trial. Built once the agent sandbox is final."""

    trial: PlannedTrial
    pack: TaskPack
    endpoint: ModelEndpoint
    budget: Budget
    escalation: EscalationPolicy
    workspace: WorkspacePaths
    agent: LeaseHandle
    """The agent sandbox that passed the preflight; may differ from ``leases.agent``."""
    leases: TrialLeases
    """Use ``open_verifier`` for the independent verifier sandbox (invariant I10)."""
    canary_token: str
    instruction: str
    scratch_dir: Path
    """Local directory the evaluator may write to; removed when the trial ends."""


RubricVerdict = Literal["pass", "fail", "inconclusive"]


@dataclass(frozen=True)
class RubricItemResult:
    """The verdict on one rubric item in one evaluation."""

    item_id: str
    verdict: RubricVerdict
    evidence: tuple[Evidence, ...] = ()
    partial: bool = False
    """A failing item that improved since the last round (defect progress ``partial``)."""


@dataclass(frozen=True)
class CheatSignal:
    """A cheating indicator (test tampering, reward writes, answer lookup, judge injection)."""

    kind: str
    detail: str = ""
    evidence_uri: str | None = None


@dataclass(frozen=True)
class Artifact:
    """A declared output of the agent, exported and sanitised (invariant I11)."""

    path: str
    sha256: str
    size_bytes: int = 0
    local_path: Path | None = None
    """Where the evaluator stored the exported copy; the runner scans it for canary tokens."""


@dataclass(frozen=True)
class EvaluationResult:
    """The outcome of one observation (``final=False``) or the final verification."""

    hard_gates: tuple[HardGate, ...]
    """All hard gates of the pack. An empty tuple counts as "not passed"."""
    rubric: tuple[RubricItemResult, ...] = ()
    cheat_signals: tuple[CheatSignal, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    verifier_lease_id: str | None = None
    manual_reasons: tuple[str, ...] = ()
    """Non-empty means the verification is unstable or the judge unusable: pause for a human."""
    judge_cost_usd: float = 0.0
    judge_consistency: float | None = None
    judge_samples: int = 0

    @property
    def hard_gates_passed(self) -> bool:
        return bool(self.hard_gates) and all(gate.passed for gate in self.hard_gates)

    @property
    def failing_item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.rubric if item.verdict == "fail")

    @property
    def partial_item_ids(self) -> tuple[str, ...]:
        return tuple(
            item.item_id for item in self.rubric if item.verdict == "fail" and item.partial
        )

    @property
    def inconclusive_item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.rubric if item.verdict == "inconclusive")
