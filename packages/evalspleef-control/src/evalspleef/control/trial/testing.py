"""Test double for the ``Evaluator`` interface: replays prepared results, one per observation.

The double behaves like a real evaluator where the runner can notice it: the final call opens
the verifier sandbox through ``ctx.leases`` and reports its lease id.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from evalspleef.core.grading import HardGate

from .types import (
    Artifact,
    CheatSignal,
    EvaluationResult,
    RubricItemResult,
    TrialContext,
)

__all__ = ["ScriptedEvaluator", "gate", "make_result"]


def gate(check_id: str, passed: bool = True) -> HardGate:
    return HardGate(check_id=check_id, passed=passed)


def make_result(
    *,
    failing: Sequence[str] = (),
    partial: Sequence[str] = (),
    inconclusive: Sequence[str] = (),
    gates_pass: bool | None = None,
    gate_ids: Sequence[str] = ("HG1", "HG2"),
    cheat: Sequence[CheatSignal] = (),
    manual_reasons: Sequence[str] = (),
    artifacts: Sequence[Artifact] = (),
    judge_consistency: float | None = None,
) -> EvaluationResult:
    """An evaluation result from rubric item ids; hard gates pass unless an item fails."""
    passed = not failing if gates_pass is None else gates_pass
    rubric = (
        *(RubricItemResult(item, "fail", partial=item in partial) for item in failing),
        *(RubricItemResult(item, "inconclusive") for item in inconclusive),
    )
    return EvaluationResult(
        hard_gates=tuple(gate(check_id, passed) for check_id in gate_ids),
        rubric=rubric,
        cheat_signals=tuple(cheat),
        artifacts=tuple(artifacts),
        manual_reasons=tuple(manual_reasons),
        judge_consistency=judge_consistency,
        judge_samples=5 if judge_consistency is not None else 0,
    )


class ScriptedEvaluator:
    """Returns ``observations[i]`` for the i-th non-final call (the last one repeats)."""

    def __init__(
        self,
        observations: Sequence[EvaluationResult],
        final: EvaluationResult | None = None,
        *,
        open_verifier: bool = True,
    ) -> None:
        if not observations:
            raise ValueError("a scripted evaluator needs at least one observation")
        self._observations = list(observations)
        self._final = final
        self._open_verifier = open_verifier
        self.calls: list[tuple[int, bool]] = []
        """``(round_no, final)`` of every call, in order."""

    async def evaluate(self, ctx: TrialContext, round_no: int, final: bool) -> EvaluationResult:
        self.calls.append((round_no, final))
        if not final:
            seen = sum(1 for _, was_final in self.calls if not was_final)
            return self._observations[min(seen, len(self._observations)) - 1]
        result = self._final if self._final is not None else self._observations[-1]
        if self._open_verifier:
            verifier = await ctx.leases.open_verifier()
            result = replace(result, verifier_lease_id=verifier.lease_id)
        return result
