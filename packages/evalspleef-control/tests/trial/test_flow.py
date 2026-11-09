"""Trial flow from lease to grade: tiers, feedback, budget, cheating, events, transitions."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from evalspleef.control.trial import CheatSignal
from evalspleef.control.trial.runner import KEY_EXPIRY_BUFFER_S
from evalspleef.control.trial.testing import make_result
from evalspleef.spi.runner import StopReason
from evalspleef.testing.fake_gateway import TrialKeyRejected
from evalspleef.testing.scripted_runner import ScriptedRunner

ITEM_ID = re.compile(r"\bR\d{1,3}\b")

HAPPY_EVENTS = [
    "canary.injected",
    "preflight.result",
    "trial.started",
    "round.started",
    "agent.step",
    "agent.stopped",
    "observe.started",
    "hardgate.result",
    "defect.updated",
    "verify.started",
    "hardgate.result",
    "hiddentest.result",
    "grade.computed",
    "canary.scanned",
]


async def test_happy_path_reaches_tier_p(harness_factory: Any, rnd: Any, legal: Any) -> None:
    h = await harness_factory(rounds=[rnd()])
    outcome = await h.run()

    assert outcome.status == "scored"
    record = outcome.record
    assert record is not None
    assert (record["state"], record["validity"]) == ("scored", "valid")
    grade = record["grades"][0]
    assert (grade["tier"], grade["grade_version"]) == ("P", 1)
    assert grade["decision"]["rule_id"] == "G-F0-PERFECT"
    assert h.evaluator.calls == [(1, False), (1, True)]
    assert h.scripted.received_messages == [h.scripted.instruction]
    assert legal(await h.log()) == [
        ("start", "leased", "running"),
        ("stop", "running", "observing"),
        ("finish", "observing", "verifying"),
        ("score", "verifying", "scored"),
    ]
    assert record["canary_token"] == h.registry.token_of(h.trial.trial_id)


async def test_trial_versions_are_recorded_when_the_agent_starts(
    harness_factory: Any, rnd: Any
) -> None:
    seed = {
        "model_version": "demo-model@3+0123abcd",
        "model_source": "huggingface",
        "hf_repo": "org/demo",
        "hf_revision": "0123abcd",
        "deployment_adapter": "novita-dedicated",
        "gateway_version": "gw-9",
        "freeze_hash": "sha256:" + "f" * 64,
        "not_a_version_field": "dropped",
    }
    h = await harness_factory(rounds=[rnd()], setup_versions=seed)
    outcome = await h.run()

    (started,) = await h.of_type("trial.started")
    versions = started.payload["versions"]
    # The control plane's model- and batch-level fields pass through unchanged.
    for key in ("model_version", "hf_repo", "hf_revision", "deployment_adapter", "freeze_hash"):
        assert versions[key] == seed[key]
    assert "not_a_version_field" not in versions
    # The runner adds what only the trial knows.
    assert versions["task_version"] == h.pack.version
    assert versions["rubric_version"] == h.pack.rubric.rubric_version
    assert versions["shell_version"] == f"{h.scripted.name}@{h.scripted.version}"
    assert versions["image_digest"] == versions["template_digest"] == h.trial.template.digest
    assert versions["provider_instance"] == "alpha"
    assert versions["sandbox_adapter"] == h.providers["alpha"].name
    assert versions["endpoint_id"] == h.trial.endpoint_id
    assert versions["grader_version"].startswith("evalspleef-core@")
    assert outcome.record is not None
    record_versions = outcome.record["extensions"]["versions"]
    # The verifier opened after trial.started joins the record's execution environment.
    assert {k: v for k, v in record_versions.items() if k != "execution_environment"} == {
        k: v for k, v in versions.items() if k != "execution_environment"
    }
    assert set(record_versions["execution_environment"]["sandboxes"]) == {"agent", "verifier"}


async def test_the_execution_environment_is_recorded_when_the_agent_starts(
    harness_factory: Any, rnd: Any
) -> None:
    h = await harness_factory(rounds=[rnd()])
    await h.run()

    (started,) = await h.of_type("trial.started")
    environment = started.payload["versions"]["execution_environment"]
    # The provider's own report, collected from the instance the agent runs on.
    assert environment["provider"]["status"] == "reported"
    assert environment["provider"]["runtime"] == {"name": "mock-sandbox", "version": "1.0.0"}
    # This harness hands the runner leases that were never measured: recorded, with a reason.
    agent = environment["sandboxes"]["agent"]
    assert agent["provider_instance"] == "alpha"
    assert agent["status"] == "unmeasured" and agent["reason"]


async def test_each_event_type_appears_in_order(harness_factory: Any, rnd: Any) -> None:
    h = await harness_factory(rounds=[rnd()], owns_terminal_events=True)
    await h.run()
    log = await h.log()
    assert [event.type.value for event in log] == [
        *HAPPY_EVENTS,
        "trial.scored",
        "teardown.started",
        "teardown.completed",
    ]
    assert {event.trial_id for event in log} == {h.trial.trial_id}
    assert {event.batch_id for event in log} == {h.trial.batch_id}


async def test_driver_mode_leaves_terminal_events_and_sandboxes_to_the_driver(
    harness_factory: Any, rnd: Any
) -> None:
    h = await harness_factory(rounds=[rnd()])
    await h.run()
    assert await h.types() == HAPPY_EVENTS
    assert h.sandboxes()
    assert all(box.alive for box in [*h.sandboxes(), *h.verifier_sandboxes])


async def test_standalone_mode_destroys_both_sandboxes(harness_factory: Any, rnd: Any) -> None:
    h = await harness_factory(rounds=[rnd()], owns_terminal_events=True)
    await h.run()
    assert len(h.verifier_sandboxes) == 1
    assert all(box.destroyed for box in [*h.sandboxes(), *h.verifier_sandboxes])
    (completed,) = await h.of_type("teardown.completed")
    assert completed.payload["ok"] is True


class KeyProbingRunner(ScriptedRunner):
    """Checks, while the agent runs, that its key is valid and carries the task budget."""

    def __init__(self, gateway: Any, rounds: Any) -> None:
        super().__init__(rounds)
        self._gateway = gateway
        self.seen: Any = None

    async def send(self, message: str, *, deadline_s: int) -> Any:
        self.seen = self._gateway.authenticate(self.trial_key)
        return await super().send(message, deadline_s=deadline_s)


async def test_gateway_key_carries_the_budget_and_is_revoked(
    harness_factory: Any, rnd: Any, clock: Any
) -> None:
    probe = KeyProbingRunner(None, [rnd()])
    h = await harness_factory(scripted=probe)
    probe._gateway = h.gateway
    await h.run()

    budget = h.pack.budget
    key = probe.seen
    assert (key.max_budget_usd, key.max_tokens) == (budget.max_cost_usd, budget.max_total_tokens)
    assert key.trial_id == h.trial.trial_id
    expires = datetime.fromisoformat(key.expires_at).timestamp()
    assert expires == clock.now() + budget.max_wall_clock_s + KEY_EXPIRY_BUFFER_S
    assert probe.model_alias == "ep_demo_00000000"
    assert probe.gateway_url == "http://gw.test/v1"
    with pytest.raises(TrialKeyRejected, match="revoked"):
        h.gateway.authenticate(probe.trial_key)


@pytest.mark.parametrize(
    ("extra_defects", "tier", "rule"),
    [(0, "B", "G-F1-B"), (2, "B+", "G-F1-BPLUS")],
)
async def test_two_round_f1_fix_scores_b_or_b_plus(
    harness_factory: Any,
    rnd: Any,
    legal: Any,
    blocking: str,
    minors: list[str],
    extra_defects: int,
    tier: str,
    rule: str,
) -> None:
    failing = [blocking, *minors[:extra_defects]]
    h = await harness_factory(
        observations=[make_result(failing=failing), make_result()],
        rounds=[rnd(), rnd(files={"fix.txt": "fixed\n"})],
    )
    outcome = await h.run()

    assert outcome.status == "scored"
    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == (tier, rule)
    assert grade["feedback_summary"]["max_level"] == "F1"
    assert grade["feedback_summary"]["feedback_rounds"] == 1
    assert grade["defect_summary"]["total"] == 1 + extra_defects
    assert grade["defect_summary"]["requiring_feedback"] == 1
    # The user message is the pack's symptom text, with no rubric id in it.
    message = h.scripted.received_messages[1]
    assert message == h.pack.defects.symptom_of(blocking)
    assert not ITEM_ID.search(message)
    flow = outcome.record["extensions"]["flow"]
    assert [item["level"] for item in flow["feedback"]] == ["F1"]
    assert flow["usage"]["feedback_rounds"] == 1
    assert legal(await h.log()) == [
        ("start", "leased", "running"),
        ("stop", "running", "observing"),
        ("needs_feedback", "observing", "awaiting_feedback"),
        ("deliver", "awaiting_feedback", "running"),
        ("stop", "running", "observing"),
        ("finish", "observing", "verifying"),
        ("score", "verifying", "scored"),
    ]


@pytest.mark.parametrize("budget", [{"max_rounds": 2}, {"max_feedback_rounds": 1}])
async def test_budget_exhaustion_scores_f(
    harness_factory: Any, rnd: Any, legal: Any, blocking: str, budget: dict[str, int]
) -> None:
    h = await harness_factory(
        observations=[make_result(failing=[blocking])],
        rounds=[rnd(), rnd(), rnd()],
        budget=budget,
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-BUDGET-F")
    assert "budget_exhausted" in grade["flags"]
    assert len(h.scripted.received_messages) == 2  # the instruction and one feedback message
    assert h.evaluator.calls[-1] == (2, True)
    assert legal(await h.log())[-2:] == [
        ("finish", "observing", "verifying"),
        ("score", "verifying", "scored"),
    ]
    exhausted = [
        e.payload["dimension"]
        for e in await h.of_type("budget.threshold")
        if e.payload["threshold"] == 1.0
    ]
    assert exhausted == [next(iter(budget)).removeprefix("max_")]


async def test_cheat_signal_scores_f_with_flags(
    harness_factory: Any, rnd: Any, blocking: str
) -> None:
    cheat = CheatSignal("test_tamper", "the protected tests changed")
    h = await harness_factory(
        observations=[make_result(failing=[blocking], cheat=[cheat])], rounds=[rnd(), rnd()]
    )
    outcome = await h.run()

    assert outcome.status == "scored"
    record = outcome.record
    grade = record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-CHEAT-01")
    assert {"cheat", "zeroed"} <= set(grade["flags"])
    assert grade["penalty"] == 0
    assert (record["validity"], grade["counted_in_leaderboard"]) == ("valid", True)
    assert len(h.scripted.received_messages) == 1  # a cheater gets no feedback round
    (incident,) = await h.of_type("incident.opened")
    assert incident.payload["kind"] == "cheat"
    assert incident.payload["signals"][0]["kind"] == "test_tamper"


@pytest.mark.parametrize("reason", [StopReason.COST_CAP, StopReason.TOKEN_CAP, StopReason.TIMEOUT])
async def test_budget_stop_reasons_are_observed_then_verified(
    harness_factory: Any, rnd: Any, blocking: str, reason: StopReason
) -> None:
    h = await harness_factory(
        observations=[make_result(failing=[blocking])], rounds=[rnd(stop_reason=reason)]
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-BUDGET-F")
    assert outcome.record["extensions"]["flow"]["stop_reasons"] == [reason.value]
    types = await h.types()
    assert types.index("agent.stopped") < types.index("observe.started")
    assert types.index("observe.started") < types.index("verify.started")
    assert "feedback.decided" not in types
    assert len(h.scripted.received_messages) == 1


async def test_loop_detected_ends_the_trial_early(
    harness_factory: Any, rnd: Any, blocking: str
) -> None:
    h = await harness_factory(
        observations=[make_result(failing=[blocking])],
        rounds=[rnd(stop_reason=StopReason.LOOP_DETECTED)],
    )
    grade = (await h.run()).record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-ABORT-04")
    assert "terminated_early" in grade["flags"]


@pytest.mark.parametrize("scenario", ["happy", "f1", "budget", "cheat"])
async def test_every_logged_transition_is_legal(
    harness_factory: Any,
    rnd: Any,
    legal: Callable[..., Any],
    blocking: str,
    scenario: str,
) -> None:
    options: dict[str, Any] = {
        "happy": {"rounds": [rnd()]},
        "f1": {
            "observations": [make_result(failing=[blocking]), make_result()],
            "rounds": [rnd(), rnd()],
        },
        "budget": {
            "observations": [make_result(failing=[blocking])],
            "rounds": [rnd(), rnd()],
            "budget": {"max_rounds": 2},
        },
        "cheat": {
            "observations": [make_result(cheat=[CheatSignal("reward_write")])],
            "rounds": [rnd()],
        },
    }
    h = await harness_factory(owns_terminal_events=True, **options[scenario])
    await h.run()
    log = await h.log()
    path = legal(log)
    assert path[0][:2] == ("start", "leased")
    assert path[-1][2] == "scored"
    carriers = {event.type.value for event in log if "transition" in event.payload}
    assert carriers <= {
        "trial.started",
        "agent.stopped",
        "defect.updated",
        "feedback.sent",
        "verify.started",
        "grade.computed",
    }
