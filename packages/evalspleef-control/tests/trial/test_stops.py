"""Stop reasons, nudges, clarifications, crash retry and the clock-driven timeouts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evalspleef.control.trial.feedback_text import _parse_persona
from evalspleef.control.trial.testing import make_result
from evalspleef.core.feedback import F0Policy
from evalspleef.spi.runner import ShellTurn, StopReason
from evalspleef.testing.scripted_runner import ScriptedRunner


class HangingRunner(ScriptedRunner):
    """An agent that never answers; only the wall-clock deadline ends its turn."""

    def __init__(self) -> None:
        super().__init__()
        self.deadline_seen: int | None = None
        self.cancelled = False

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        self.deadline_seen = deadline_s
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("a hanging agent never returns")


class HangingEvaluator:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def evaluate(self, ctx: Any, round_no: int, final: bool) -> Any:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("a hanging evaluator never returns")


async def test_wall_clock_timeout_stops_the_agent(
    harness_factory: Any, clock: Any, blocking: str, legal: Any
) -> None:
    hanging = HangingRunner()
    h = await harness_factory(scripted=hanging, observations=[make_result(failing=[blocking])])
    task = asyncio.create_task(h.run())
    await asyncio.wait_for(clock.sleepers(1), timeout=10)
    clock.advance(h.pack.budget.max_wall_clock_s)
    outcome = await asyncio.wait_for(task, timeout=10)

    assert outcome.status == "scored"
    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-BUDGET-F")
    assert "budget_exhausted" in grade["flags"]
    assert hanging.cancelled
    assert hanging.deadline_seen == h.pack.budget.max_wall_clock_s
    flow = outcome.record["extensions"]["flow"]
    assert flow["stop_reasons"] == ["timeout"]
    assert flow["usage"]["wall_clock_s"] >= h.pack.budget.max_wall_clock_s
    (stopped,) = await h.of_type("agent.stopped")
    assert stopped.payload["stop_reason"] == "timeout"
    assert legal(await h.log())[1] == ("stop", "running", "observing")


async def test_observation_timeout_is_an_infrastructure_failure(
    harness_factory: Any, clock: Any, rnd: Any
) -> None:
    hanging = HangingEvaluator()
    h = await harness_factory(rounds=[rnd()], evaluator=hanging)
    task = asyncio.create_task(h.run())
    await asyncio.wait_for(hanging.entered.wait(), timeout=10)
    await asyncio.wait_for(clock.sleepers(1), timeout=10)  # the observation's own timer
    clock.advance(h.pack.budget.max_observe_s)
    outcome = await asyncio.wait_for(task, timeout=10)

    assert outcome.status == "failed_infra"
    assert outcome.reason.startswith("EvaluationTimeout")
    with pytest.raises(Exception, match="revoked"):
        h.gateway.authenticate(h.scripted.trial_key)


async def test_idle_agent_gets_an_f0_nudge_that_costs_nothing(
    harness_factory: Any, rnd: Any
) -> None:
    h = await harness_factory(
        observations=[make_result(gates_pass=False), make_result()],
        rounds=[rnd(stop_reason=StopReason.IDLE), rnd()],
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("P", "G-F0-PERFECT")
    assert grade["feedback_summary"]["nudges_used"] == 1
    assert grade["feedback_summary"]["feedback_rounds"] == 0
    nudge = h.scripted.received_messages[1]
    assert 0 < len(nudge) <= 60
    (record,) = outcome.record["extensions"]["flow"]["feedback"]
    assert (record["kind"], record["level"], record["target_defect_id"]) == ("nudge", "F0", None)


async def test_nudges_are_capped_then_the_trial_aborts(harness_factory: Any, rnd: Any) -> None:
    idle = rnd(stop_reason=StopReason.IDLE)
    h = await harness_factory(
        observations=[make_result(gates_pass=False)], rounds=[idle, idle, idle, idle]
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-ABORT-04")
    assert len(h.scripted.received_messages) == 3  # instruction plus max_nudges nudges
    assert outcome.record["extensions"]["flow"]["usage"]["nudges"] == h.pack.budget.max_nudges


def _faq_entry(pack: Any) -> tuple[str, str]:
    _, faq = _parse_persona((pack.root / "persona.md").read_text(encoding="utf-8"))
    question, answer = next(iter(faq.items()))
    assert answer != F0Policy.GENERIC_ANSWER
    return question, answer


async def test_clarification_is_answered_from_the_faq_without_counting(
    harness_factory: Any, rnd: Any, pack: Any
) -> None:
    question, answer = _faq_entry(pack)
    h = await harness_factory(
        rounds=[rnd(stop_reason=StopReason.NEED_CLARIFICATION, final_message=question), rnd()]
    )
    outcome = await h.run()

    assert h.scripted.received_messages[1] == answer
    grade = outcome.record["grades"][0]
    assert grade["tier"] == "P"
    usage = outcome.record["extensions"]["flow"]["usage"]
    assert (usage["rounds"], usage["feedback_rounds"], usage["nudges"]) == (1, 0, 0)
    assert outcome.record["extensions"]["flow"]["feedback"] == []


async def test_clarifications_are_limited_to_three(
    harness_factory: Any, rnd: Any, pack: Any
) -> None:
    question, _ = _faq_entry(pack)
    ask = rnd(stop_reason=StopReason.NEED_CLARIFICATION, final_message=question)
    h = await harness_factory(rounds=[ask, ask, ask, ask])
    outcome = await h.run()

    assert outcome.status == "scored"
    assert len(h.scripted.received_messages) == 4  # the fourth question is treated as a stop
    assert outcome.record["extensions"]["flow"]["usage"]["rounds"] == 1


async def test_one_crash_is_retried_on_the_same_sandbox(harness_factory: Any, rnd: Any) -> None:
    h = await harness_factory(rounds=[rnd(stop_reason=StopReason.CRASH), rnd()])
    outcome = await h.run()

    assert outcome.status == "scored"
    assert h.scripted.received_messages[0] == h.scripted.received_messages[1]
    assert outcome.record["extensions"]["flow"]["usage"]["rounds"] == 1


async def test_a_second_crash_is_an_infrastructure_failure(harness_factory: Any, rnd: Any) -> None:
    crash = rnd(stop_reason=StopReason.CRASH)
    h = await harness_factory(rounds=[crash, crash], owns_terminal_events=True)
    outcome = await h.run()

    assert outcome.status == "failed_infra"
    assert outcome.reason.startswith("RunnerCrashed")
    assert all(box.destroyed for box in h.sandboxes())
