from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evalspleef.control.trial.runner import verification_events
from evalspleef.core.events import EventType
from evalspleef.core.models import CheckKind, Evidence, EvidenceKind


def _pack(**kinds: CheckKind) -> Any:
    items = [SimpleNamespace(id=item_id, kind=kind) for item_id, kind in kinds.items()]
    return SimpleNamespace(rubric=SimpleNamespace(items=items))


def _final(verdicts: dict[str, str], **judge: Any) -> Any:
    rubric = [
        SimpleNamespace(item_id=item_id, verdict=v, evidence=()) for item_id, v in verdicts.items()
    ]
    defaults = {
        "judge_consistency": None,
        "judge_samples": 0,
        "judge_cost_usd": 0.0,
        "judge_votes": (),
    }
    return SimpleNamespace(rubric=rubric, **{**defaults, **judge})


def test_script_items_are_the_hidden_test_result_and_judge_items_the_verdict() -> None:
    pack = _pack(
        R1=CheckKind.SCRIPT, R2=CheckKind.SCRIPT, R3=CheckKind.LLM_JUDGE, R4=CheckKind.AGENT_JUDGE
    )
    final = _final(
        {"R1": "pass", "R2": "fail", "R3": "pass", "R4": "inconclusive"},
        judge_consistency=0.9,
        judge_samples=3,
        judge_cost_usd=0.01,
    )
    (hidden_type, hidden), (judge_type, judge) = verification_events(pack, final)
    assert hidden_type is EventType.HIDDENTEST_RESULT
    assert hidden["items"] == [
        {"item_id": "R1", "verdict": "pass"},
        {"item_id": "R2", "verdict": "fail"},
    ]
    assert (hidden["passed"], hidden["total"]) == (1, 2)
    assert judge_type is EventType.JUDGE_VERDICT
    assert judge["items"] == [{"item_id": "R3", "verdict": "pass"}]
    assert (judge["agreement"], judge["samples"], judge["cost_usd"]) == (0.9, 3, 0.01)


def test_a_pack_without_judge_items_emits_no_judge_verdict() -> None:
    events = verification_events(_pack(R1=CheckKind.SCRIPT), _final({"R1": "pass"}))
    assert [event_type for event_type, _ in events] == [EventType.HIDDENTEST_RESULT]


def test_agent_judge_verdicts_reference_their_transcripts() -> None:
    pack = _pack(R1=CheckKind.SCRIPT, R11=CheckKind.AGENT_JUDGE)
    final = _final({"R1": "pass", "R11": "fail"})
    transcript = Evidence(EvidenceKind.OTHER, uri="/runs/t1/R11.json", sha256="ab" * 32)
    final.rubric[1].evidence = (transcript,)
    events = dict(verification_events(pack, final))
    judge = events[EventType.JUDGE_VERDICT]
    assert judge["items"] == []
    assert judge["agent_items"] == [
        {
            "item_id": "R11",
            "verdict": "fail",
            "transcripts": [{"uri": "/runs/t1/R11.json", "sha256": "ab" * 32}],
        }
    ]


def test_judge_verdicts_carry_per_judge_votes() -> None:
    """llm_judge items carry per-judge votes (decision + per-sample votes) for agreement recomputation."""
    from evalspleef.control.trial.types import JudgeItemVotes, JudgeVote

    pack = _pack(R1=CheckKind.LLM_JUDGE, R2=CheckKind.LLM_JUDGE)
    final = _final({"R1": "pass", "R2": "inconclusive"})
    final.judge_votes = (
        JudgeItemVotes(
            "R1",
            {
                "judge-a": JudgeVote("pass", (True, True, True, True, False)),
                "judge-b": JudgeVote("fail", (False, False, True, False, False)),
            },
        ),
        JudgeItemVotes(
            "R2",
            {
                "judge-a": JudgeVote("inconclusive", (None, None, True, None, None)),
                "judge-b": JudgeVote("inconclusive", (None, None, None, None, None)),
            },
        ),
    )
    events = dict(verification_events(pack, final))
    judge = events[EventType.JUDGE_VERDICT]
    assert judge["items"][0] == {
        "item_id": "R1",
        "verdict": "pass",
        "judge_votes": {
            "judge-a": {"decision": "pass", "samples": [True, True, True, True, False]},
            "judge-b": {"decision": "fail", "samples": [False, False, True, False, False]},
        },
    }
    assert judge["items"][1]["judge_votes"]["judge-a"]["decision"] == "inconclusive"
