"""``agent_judge`` rubric items through the shared judge panel (PRD 11.1, 03-grading 8.6).

:class:`AgentJudgeStep` sits next to :class:`~.judging.JudgeStep`. For every agent_judge
item it builds a :class:`evalspleef.core.judge.JudgePanel` whose clients each wrap one full
agent session as ``complete(prompt)``: k-sampling, the family rule, ``inconclusive`` and
vote aggregation stay in core. Each sample runs in its own judge sandbox (see
:mod:`.agent_bridge` and :mod:`.agent_session`); a failed sample is an invalid vote, and too
many invalid votes send the item to the manual queue, as with a chat judge.

The transcript of every item (each action, the truncated output, the sha256 of the full
output, the verdicts) is written as a JSON artifact and referenced from the item's result
with its sha256; ``judge.verdict`` carries those references. No event type is added.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from evalspleef.core.judge import ItemResult, JudgePanel
from evalspleef.core.models import Evidence, EvidenceKind, RubricItem
from evalspleef.spi.gateway import PriceSpec

from ..trial.types import RubricItemResult, RubricVerdict, TrialContext
from .agent_bridge import JudgeSandboxOpener, RunSetup, SampleRunner
from .agent_plan import judge_zones
from .agent_records import SampleRecord, UsageMeter, tree_manifest
from .agent_session import AgentJudgeLimits, AgentSession, ChatJudge, SampleSpec
from .artifacts import WorkspaceExport
from .judging import JudgeRun

__all__ = [
    "AgentJudgeRun",
    "AgentJudgeSettings",
    "AgentJudgeStep",
    "judge_totals",
]


@dataclass(frozen=True)
class AgentJudgeSettings:
    """Batch-level options of the agent judge (``agent_judge`` in batch.yaml)."""

    target_family: str
    k: int = 3
    limits: AgentJudgeLimits = field(default_factory=AgentJudgeLimits)
    allow_single_vendor: bool = False
    run_every_round: bool = False
    """``False``: only the final verification runs judge agents."""
    prices: Mapping[str, PriceSpec] = field(default_factory=dict)
    transcript_root: Path | None = None
    """Where transcripts go; ``None`` puts them under the trial scratch directory."""


@dataclass(frozen=True)
class AgentJudgeRun:
    """The agent panel outcome for one evaluation."""

    results: tuple[RubricItemResult, ...]
    manual_queue: tuple[str, ...]
    agreement: float | None
    decided: int
    """Items outside the manual queue, the weight of ``agreement``."""
    samples: int
    cost_usd: float
    injection_attempts: int
    """Distinct injection phrases seen plus samples whose /artifact changed (8.6)."""
    session_id: str


class _AgentPanelClient:
    """A judge client whose ``complete()`` is one full agent session on a bound item."""

    def __init__(self, runner: SampleRunner, session: AgentSession, base: SampleSpec) -> None:
        self._runner = runner
        self._session = session
        self._base = base
        self._count = 0
        self.name = session.model.name
        self.provider = session.model.provider
        self.family = session.model.family

    def complete(self, prompt: str) -> str:
        del prompt  # the item is bound; the session builds its own prompt
        self._count += 1
        spec = SampleSpec(self._base.item, self._base.vendor, self._count, self._base.session_id)
        record = self._runner.run_sample(self._session, spec)
        # "inconclusive" does not parse as pass/fail, so the panel counts an invalid vote.
        return json.dumps({"verdict": record.verdict})


class AgentJudgeStep:
    """Runs judge agents over agent_judge items. Holds no per-trial state."""

    def __init__(
        self,
        clients: Mapping[str, ChatJudge],
        opener: JudgeSandboxOpener,
        settings: AgentJudgeSettings,
    ) -> None:
        if not clients:
            raise ValueError("an agent judge step needs at least one judge client")
        self._clients = dict(clients)
        self._opener = opener
        self.settings = settings

    async def run(
        self, ctx: TrialContext, export: WorkspaceExport, items: Sequence[RubricItem]
    ) -> AgentJudgeRun:
        """Sample every item; sandboxes are destroyed before this returns or raises."""
        expected = await asyncio.to_thread(tree_manifest, export.tree)
        limits = self.settings.limits
        setup = RunSetup(
            self._opener, ctx.trial, export.tree, expected, judge_zones(export), limits
        )
        evaluation = _Evaluation(
            self._clients, self.settings, ctx, SampleRunner(asyncio.get_running_loop(), setup)
        )
        try:
            judged = await asyncio.to_thread(evaluation.judge, tuple(items))
        except BaseException:
            await evaluation.runner.cancel()
            raise
        await evaluation.runner.drain()
        results = await asyncio.gather(*(evaluation.transcript(*entry) for entry in judged))
        manual = tuple(result.item_id for _i, result, _r in judged if result.manual_required)
        agreements = [result.agreement for _i, result, _r in judged if not result.manual_required]
        records = evaluation.runner.records
        return AgentJudgeRun(
            results=tuple(results),
            manual_queue=manual,
            agreement=sum(agreements) / len(agreements) if agreements else None,
            decided=len(agreements),
            samples=len(records),
            cost_usd=evaluation.cost(),
            injection_attempts=_injection_attempts(records),
            session_id=evaluation.session_id,
        )


_Judged = tuple[RubricItem, ItemResult, list[SampleRecord]]


class _Evaluation:
    """The state of one ``AgentJudgeStep.run``: its runner, meters and session id."""

    def __init__(
        self,
        clients: Mapping[str, ChatJudge],
        settings: AgentJudgeSettings,
        ctx: TrialContext,
        runner: SampleRunner,
    ) -> None:
        self._clients = clients
        self._settings = settings
        self._ctx = ctx
        self.runner = runner
        self.meters = {vendor: UsageMeter() for vendor in clients}
        self.session_id = uuid.uuid4().hex

    def judge(self, items: tuple[RubricItem, ...]) -> list[_Judged]:
        """Worker thread: one panel per item, so every client is bound to its item."""
        settings = self._settings
        judged: list[_Judged] = []
        for item in items:
            clients = {
                vendor: _AgentPanelClient(
                    self.runner,
                    AgentSession(model, self.meters[vendor], settings.limits),
                    SampleSpec(item, vendor, 0, self.session_id),
                )
                for vendor, model in self._clients.items()
            }
            panel = JudgePanel(
                clients,
                target_model=self._ctx.endpoint.model_alias,
                target_family=settings.target_family,
                k=settings.k,
                max_retries=0,
                allow_single_vendor=settings.allow_single_vendor,
                roles=dict.fromkeys(clients, "agent_judge"),
            )
            first = len(self.runner.records)
            result = panel.evaluate_item(item)
            judged.append((item, result, self.runner.records[first:]))
        return judged

    async def transcript(
        self, item: RubricItem, result: ItemResult, records: list[SampleRecord]
    ) -> RubricItemResult:
        """Write the item's transcript and return its verdict referencing it by sha256."""
        ctx = self._ctx
        verdict: RubricVerdict = (
            "inconclusive" if result.manual_required else ("pass" if result.passed else "fail")
        )
        document = {
            "session_id": self.session_id,
            "trial_id": ctx.trial.trial_id,
            "task_id": ctx.pack.task_id,
            "item_id": item.id,
            "verdict": verdict,
            "panel": result.to_dict(),
            "samples": [record.to_dict() for record in records],
        }
        data = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        directory = self._settings.transcript_root or ctx.scratch_dir / "agent-judge"
        path = directory / f"{ctx.pack.task_id}-{item.id}-{self.session_id}.json"
        await asyncio.to_thread(_write, path, data)
        evidence = Evidence(
            kind=EvidenceKind.OTHER,
            uri=str(path),
            sha256=hashlib.sha256(data).hexdigest(),
            caption="agent_judge transcript",
        )
        return RubricItemResult(item.id, verdict, evidence=(evidence,))

    def cost(self) -> float:
        """An explicit price table wins, else what the gateway charged (as JudgeStep)."""
        total = 0.0
        for vendor, meter in self.meters.items():
            price = self._settings.prices.get(vendor)
            if price is None:
                total += meter.cost_usd
                continue
            total += (
                meter.input_tokens * price.input_per_mtok
                + meter.output_tokens * price.output_per_mtok
            ) / 1_000_000
        return total


def judge_totals(
    judge: JudgeRun | None, agent: AgentJudgeRun | None
) -> tuple[float, float | None, int]:
    """Cost, agreement (weighted by decided items) and samples of both judge kinds."""
    weighted: list[tuple[float, int]] = []
    if judge is not None and judge.agreement is not None:
        weighted.append((judge.agreement, len(judge.results) - len(judge.manual_queue)))
    if agent is not None and agent.agreement is not None:
        weighted.append((agent.agreement, agent.decided))
    count = sum(weight for _value, weight in weighted)
    agreement = sum(value * weight for value, weight in weighted) / count if count else None
    cost = (judge.cost_usd if judge else 0.0) + (agent.cost_usd if agent else 0.0)
    samples = (judge.samples if judge else 0) + (agent.samples if agent else 0)
    return cost, agreement, samples


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _injection_attempts(records: Sequence[SampleRecord]) -> int:
    phrases = {hit for record in records for hit in record.injection_hits}
    tampered = sum(1 for record in records if record.artifact_intact is False)
    return len(phrases) + tampered
