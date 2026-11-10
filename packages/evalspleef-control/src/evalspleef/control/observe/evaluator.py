"""The evaluator behind the trial runner: export, verify, map, judge.

One evaluation runs the full pipeline:

1. export the agent workspace from the agent sandbox and sanitise it (I11);
2. open the independent verifier sandbox (``ctx.leases.open_verifier()``, I10) and
   upload the pack's ``hidden-tests/`` plus the sanitised tree — hidden tests never
   touch the agent sandbox;
3. run every hard gate of task.yaml there, deleting stale reward files first (I12);
4. recompute the pack's hash locks on the sanitised export — mismatches are cheat
   signals, independent of gate exit codes (I12);
5. map gate results onto the pack's script-kind rubric items
   (:mod:`...observe.mapping`);
6. send ``llm_judge`` items to the judge panel through the gateway (I15);
7. send ``agent_judge`` items to the agent judge (:mod:`...observe.agent_judge`), one fresh
   judge sandbox per sample, on the final verification unless configured for every round.

``Evaluator.for_regrade`` builds the verify-only mode used to regrade stored
artifacts: the export step reads a sanitised tree from local disk instead of the
agent sandbox, everything downstream is identical.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from evalspleef.core.judge import JudgeClient
from evalspleef.spi.gateway import PriceSpec

from ..batch.executor import LeaseHandle
from ..taskpack.models import TaskPack
from ..trial.types import CheatSignal, EvaluationResult, RubricItemResult, TrialContext
from .agent_judge import AgentJudgeRun, AgentJudgeStep, judge_totals
from .agent_plan import split_agent_items
from .artifacts import (
    REASON_MISSING,
    ExportCaps,
    WorkspaceExport,
    artifact_decls,
    export_workspace,
    protected_paths,
    sanitise_tree,
)
from .judging import JudgeRun, JudgeStep
from .mapping import map_script_items
from .verifier import (
    HashLockMismatch,
    VerificationOutput,
    check_hash_locks,
    find_hash_manifest,
    requires_host_tools,
    run_verification,
)

__all__ = ["ArtifactSource", "Evaluator", "SandboxSource", "StoredArtifactSource"]

CHEAT_HASH_LOCK = "hash_lock"
CHEAT_SMUGGLE = "artifact_smuggle"
CHEAT_REWARD = "reward_planted"
#: 03-grading 8.6: three or more judge injection attempts send the trial to a human.
INJECTION_MANUAL_THRESHOLD = 3


class ArtifactSource(Protocol):
    """Where the sanitised candidate tree comes from."""

    async def export(self, ctx: TrialContext, dest: Path) -> WorkspaceExport:
        """Produce the sanitised tree at ``dest``."""
        ...


class SandboxSource:
    """Live export from the agent sandbox (normal trial operation)."""

    def __init__(self, caps: ExportCaps | None = None) -> None:
        self._caps = caps

    async def export(self, ctx: TrialContext, dest: Path) -> WorkspaceExport:
        return await export_workspace(
            ctx.agent.sandbox,
            ctx.workspace.remote_root,
            ctx.pack,
            dest,
            caps=self._caps,
        )


class StoredArtifactSource:
    """Verify-only source: regrade a stored sanitised export from local disk."""

    def __init__(self, tree: Path) -> None:
        self._tree = tree

    async def export(self, ctx: TrialContext, dest: Path) -> WorkspaceExport:
        pack = ctx.pack
        return await asyncio.to_thread(
            sanitise_tree,
            self._tree,
            dest,
            artifact_decls(pack),
            protected=protected_paths(pack),
        )


class Evaluator:
    """Implements the ``Evaluator`` protocol of :mod:`...trial.protocols`."""

    def __init__(
        self,
        *,
        source: ArtifactSource | None = None,
        judges: Mapping[str, JudgeClient] | None = None,
        target_family: str = "",
        judge_k: int = 5,
        judge_max_retries: int = 2,
        judge_max_parallel_per_vendor: int = 5,
        judge_prices: Mapping[str, PriceSpec] | None = None,
        allow_single_judge: bool = False,
        verify_root: str = "/verify",
        host_tools: Callable[[TaskPack], str | None] | None = None,
        agent_judge: AgentJudgeStep | None = None,
    ) -> None:
        self._source = source or SandboxSource()
        self._judges = dict(judges or {})
        self._target_family = target_family
        self._judge_k = judge_k
        self._judge_max_retries = judge_max_retries
        self._judge_max_parallel_per_vendor = judge_max_parallel_per_vendor
        self._judge_prices = dict(judge_prices or {})
        self._allow_single_judge = allow_single_judge
        self._verify_root = verify_root
        self._host_tools = host_tools or requires_host_tools
        self._agent_judge = agent_judge

    @classmethod
    def for_regrade(cls, artifact_dir: str | Path, **kwargs: Any) -> Evaluator:
        """Verify-only evaluator for regrading stored (sanitised) artifacts.

        Regrade never re-runs a judge agent: grading reads the recorded verdicts.
        """
        if kwargs.get("agent_judge") is not None:
            raise ValueError("a regrade never re-runs a judge agent")
        return cls(source=StoredArtifactSource(Path(artifact_dir)), **kwargs)

    async def evaluate(self, ctx: TrialContext, round_no: int, final: bool) -> EvaluationResult:
        """Run the pipeline; every round is verified, ``final`` only gates judge agents."""
        pack = ctx.pack
        host_reason = self._host_tools(pack)
        if host_reason is not None:
            return EvaluationResult(
                hard_gates=(),
                manual_reasons=(f"{pack.task_id}: {host_reason}",),
            )

        export = await self._source.export(ctx, ctx.scratch_dir / f"export-r{round_no}")
        verifier = await ctx.leases.open_verifier()
        output = await self._verify(verifier, pack, export)

        locks = await self._hash_locks(pack, export)
        missing = [issue.path for issue in export.issues if issue.reason == REASON_MISSING]
        rubric = list(map_script_items(pack, output.gates, missing_artifacts=missing))
        judge_run, judge_manual = await self._judge(ctx, pack, export, rubric)
        agent_run, agent_manual = await self._run_agent_judge(ctx, export, rubric, final)
        self._cover_remaining_items(pack, rubric)

        manual: list[str] = [*judge_manual, *agent_manual]
        cheat = [
            *(CheatSignal(CHEAT_HASH_LOCK, detail=_lock_detail(lock)) for lock in locks),
            *(
                CheatSignal(CHEAT_SMUGGLE, detail=f"{issue.path}: {issue.reason}")
                for issue in export.cheat_clues
            ),
            *(CheatSignal(CHEAT_REWARD, detail=path) for path in output.planted_rewards),
        ]
        if agent_run is not None and agent_run.injection_attempts and cheat:
            manual.append("agent judge injection attempts together with cheat signals (8.6)")
        cost, consistency, samples = judge_totals(judge_run, agent_run)
        return EvaluationResult(
            hard_gates=output.hard_gates(),
            rubric=tuple(rubric),
            cheat_signals=tuple(cheat),
            artifacts=export.artifacts,
            verifier_lease_id=verifier.lease_id,
            manual_reasons=tuple(manual),
            judge_cost_usd=cost,
            judge_consistency=consistency,
            judge_samples=samples,
            judge_votes=() if judge_run is None else judge_run.item_votes,
        )

    async def _verify(
        self,
        verifier: LeaseHandle,
        pack: TaskPack,
        export: WorkspaceExport,
    ) -> VerificationOutput:
        sandbox = verifier.sandbox
        await sandbox.upload(pack.root / "hidden-tests", f"{self._verify_root}/hidden-tests")
        await sandbox.upload(export.tree, f"{self._verify_root}/workspace")
        return await run_verification(sandbox, pack, verify_root=self._verify_root)

    async def _hash_locks(
        self, pack: TaskPack, export: WorkspaceExport
    ) -> tuple[HashLockMismatch, ...]:
        manifest = find_hash_manifest(pack)
        if manifest is None:
            return ()
        return await asyncio.to_thread(
            check_hash_locks,
            manifest,
            export.tree,
            protected=protected_paths(pack),
        )

    async def _judge(
        self,
        ctx: TrialContext,
        pack: TaskPack,
        export: WorkspaceExport,
        rubric: list[RubricItemResult],
    ) -> tuple[JudgeRun | None, list[str]]:
        from evalspleef.core.models import CheckKind

        judge_items = [item for item in pack.rubric.items if item.kind is CheckKind.LLM_JUDGE]
        if not judge_items:
            return None, []
        if not self._judges:
            rubric.extend(RubricItemResult(item.id, "inconclusive") for item in judge_items)
            return None, ["no judge panel configured for llm_judge rubric items"]
        step = JudgeStep(
            self._judges,
            target_family=self._target_family,
            target_model=ctx.endpoint.model_alias,
            k=self._judge_k,
            max_retries=self._judge_max_retries,
            max_parallel_per_vendor=self._judge_max_parallel_per_vendor,
            prices=self._judge_prices,
            allow_single_vendor=self._allow_single_judge,
        )
        run = await asyncio.to_thread(step.run, pack, export, scratch_dir=ctx.scratch_dir / "judge")
        rubric.extend(run.results)
        manual = [f"judge manual queue: {', '.join(run.manual_queue)}"] if run.manual_queue else []
        return run, manual

    async def _run_agent_judge(
        self,
        ctx: TrialContext,
        export: WorkspaceExport,
        rubric: list[RubricItemResult],
        final: bool,
    ) -> tuple[AgentJudgeRun | None, list[str]]:
        """Judge agents for agent_judge items; every item left undecided says why."""
        runnable, manual = split_agent_items(ctx.pack)
        step = self._agent_judge
        if not runnable:
            return None, manual
        ids = ", ".join(item.id for item in runnable)
        if step is None:
            manual.append(f"agent_judge items {ids}: no agent judge configured")
            return None, manual
        if not final and not step.settings.run_every_round:
            manual.append(f"agent_judge items {ids}: judge agents run on the final verification")
            return None, manual
        run = await step.run(ctx, export, runnable)
        rubric.extend(run.results)
        if run.manual_queue:
            manual.append(f"agent judge manual queue: {', '.join(run.manual_queue)}")
        if run.injection_attempts >= INJECTION_MANUAL_THRESHOLD:
            manual.append(f"agent judge injection attempts: {run.injection_attempts} (8.6)")
        return run, manual

    @staticmethod
    def _cover_remaining_items(pack: TaskPack, rubric: list[RubricItemResult]) -> None:
        """Items no step can decide (agent_judge, human, unknown kinds) go to the
        manual queue as inconclusive — never a silent pass or fail."""
        covered = {result.item_id for result in rubric}
        for item in pack.rubric.items:
            if item.id not in covered:
                rubric.append(RubricItemResult(item.id, "inconclusive"))


def _lock_detail(lock: HashLockMismatch) -> str:
    actual = lock.actual if lock.actual is not None else "<missing>"
    return f"{lock.relative}: expected {lock.expected[:12]}…, got {actual[:12]}…"
