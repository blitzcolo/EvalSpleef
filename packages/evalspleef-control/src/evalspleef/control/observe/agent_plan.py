"""Which agent_judge items can run, and on which carrier (PLAN.md agent_judge design).

Items needing images stay inconclusive with a reason: the judge protocol is text-only and
the Anthropic upstream rejects images. A carrier hosts judge sandboxes only when it
declares both ``writable_paths`` and ``read_only_upload``; otherwise the plan is refused
with ``PolicyNotEnforceable`` rather than degraded.
"""

from __future__ import annotations

from collections.abc import Callable

from evalspleef.core.models import CheckKind, RubricItem
from evalspleef.spi.errors import PolicyNotEnforceable
from evalspleef.spi.sandbox import SandboxCapabilities, WritablePath

from ..batch.executor import PlannedTrial
from ..batch.plan import BatchPlan
from ..taskpack.models import TaskPack
from .agent_prompt import ARTIFACT_DIR
from .agent_protocol import WORK_DIR
from .artifacts import WorkspaceExport

__all__ = [
    "VISUAL_EVIDENCE",
    "agent_items",
    "judge_carrier",
    "judge_zones",
    "refuse_unfit_plan",
    "split_agent_items",
]

#: Evidence kinds a text-only judge cannot look at; such items stay inconclusive.
VISUAL_EVIDENCE = frozenset({"screenshot", "video_frame"})
_SCRATCH_MB = 256


def agent_items(pack: TaskPack) -> tuple[RubricItem, ...]:
    return tuple(item for item in pack.rubric.items if item.kind is CheckKind.AGENT_JUDGE)


def judge_zones(export: WorkspaceExport) -> tuple[WritablePath, ...]:
    """The declared memory-backed zones, sized from the export (they count against memory).

    ``/tmp`` is not declared: every carrier that declares ``writable_paths`` already mounts
    it as its own memory-backed scratch and refuses it as a declared path, and it is as new
    as the sandbox, so nothing carries over between samples.
    """
    artifact_mb = export.total_bytes // (1 << 20) + 16
    return (
        WritablePath(ARTIFACT_DIR, artifact_mb),
        WritablePath(WORK_DIR, 2 * artifact_mb + _SCRATCH_MB),
    )


def judge_carrier(trial: PlannedTrial, capabilities: Callable[[str], SandboxCapabilities]) -> str:
    """The first verifier route that can host the trial's judge sandboxes.

    Judge sandboxes use the verifier's image and network rule, so they go where the
    verifier may go. Checked at plan time and again when a judge sandbox is opened.

    Raises ``PolicyNotEnforceable`` when none declares both ``writable_paths`` and
    ``read_only_upload``: an agent judge is never run on a carrier that cannot keep the
    artifact read-only.
    """
    for instance in trial.verifier_routes:
        caps = capabilities(instance)
        if caps.writable_paths and caps.read_only_upload:
            return instance
    raise PolicyNotEnforceable(
        f"{trial.task_id} has agent_judge items but no carrier among "
        f"{list(trial.verifier_routes)} (os {trial.requirements.os}) declares "
        "writable_paths and read_only_upload"
    )


def split_agent_items(pack: TaskPack) -> tuple[list[RubricItem], list[str]]:
    """The agent_judge items a text judge can decide, and a reason for each it cannot."""
    runnable: list[RubricItem] = []
    reasons: list[str] = []
    for item in agent_items(pack):
        visual = sorted(VISUAL_EVIDENCE & set(item.evidence_required))
        if not visual:
            runnable.append(item)
            continue
        reasons.append(
            f"{item.id}: agent_judge item needs image evidence ({', '.join(visual)}); "
            "the agent judge is text-only"
        )
    return runnable, reasons


def refuse_unfit_plan(
    plan: BatchPlan,
    packs: Callable[[str], TaskPack],
    capabilities: Callable[[str], SandboxCapabilities],
) -> None:
    """Plan time: every runnable cell with agent_judge items has a judge carrier.

    Raises ``PolicyNotEnforceable`` naming the pack; called only when an agent judge is
    configured, so a batch without one keeps today's behaviour.
    """
    for cell in plan.runnable_cells:
        if cell.trials and agent_items(packs(cell.task.task_id)):
            judge_carrier(cell.trials[0], capabilities)
