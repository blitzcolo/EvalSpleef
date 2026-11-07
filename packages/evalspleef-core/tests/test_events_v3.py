"""Event envelope and event-type contract (PRD appendix B.8, FR-FLOW-04)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import pytest

from evalspleef.core.events import (
    SCHEMA_VERSION,
    EventEnvelope,
    EventType,
    UnknownEventType,
    new_event_id,
)

B8_TYPES = {
    "trial.created",
    "trial.leased",
    "sandbox.provisioned",
    "canary.injected",
    "preflight.result",
    "trial.started",
    "round.started",
    "agent.step",
    "agent.stopped",
    "observe.started",
    "hardgate.result",
    "evidence.collected",
    "defect.updated",
    "feedback.decided",
    "feedback.leakcheck",
    "feedback.sent",
    "budget.threshold",
    "verify.started",
    "hiddentest.result",
    "judge.verdict",
    "grade.computed",
    "trial.scored",
    "trial.invalidated",
    "teardown.started",
    "teardown.completed",
    "canary.scanned",
    "incident.opened",
}

FLOW_V3_TYPES = {
    "endpoint.registered",
    "endpoint.probe.result",
    "endpoint.warm",
    "endpoint.cold_start",
    "endpoint.unavailable",
    "endpoint.retired",
    "provider.capability.reject",
    "host.drained",
    "sandbox.orphan.reaped",
}


def make_envelope(**overrides: object) -> EventEnvelope:
    values: dict[str, object] = {
        "event_id": "evt_01J00000000000000000000AA",
        "ts": "2026-11-06T12:00:00Z",
        "batch_id": "2026-11-b1",
        "type": EventType.FEEDBACK_SENT,
        "actor": "controller",
        "trial_id": "web-001.claude-sonnet-5.claude-code.e1.t02",
        "payload": {"round": 2, "level": "F1", "defect_id": "R3#1", "leak_check": "pass"},
    }
    values.update(overrides)
    return EventEnvelope(**values)  # type: ignore[arg-type]


def test_every_required_event_type_is_declared() -> None:
    declared = {str(event_type) for event_type in EventType}
    assert declared >= B8_TYPES
    assert declared >= FLOW_V3_TYPES
    assert len(declared) == len(B8_TYPES) + len(FLOW_V3_TYPES)


def test_json_line_round_trip() -> None:
    envelope = make_envelope()
    line = envelope.to_json_line()
    assert "\n" not in line
    restored = EventEnvelope.from_json_line(line)
    assert restored == envelope
    assert restored.to_json_line() == line


def test_json_line_is_canonical() -> None:
    envelope = make_envelope(payload={"说明": "中文", "b": 2, "a": 1})
    line = envelope.to_json_line()
    parsed = json.loads(line)
    canonical = json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert line == canonical
    assert "说明" in line
    reordered = make_envelope(payload={"a": 1, "b": 2, "说明": "中文"})
    assert reordered.to_json_line() == line


def test_unknown_type_is_rejected_at_construction() -> None:
    with pytest.raises(UnknownEventType):
        make_envelope(type="trial.teleported")


def test_unknown_type_is_rejected_when_parsing() -> None:
    document = json.loads(make_envelope().to_json_line())
    document["type"] = "trial.teleported"
    with pytest.raises(UnknownEventType):
        EventEnvelope.from_json_line(json.dumps(document))


def test_malformed_lines_are_rejected() -> None:
    with pytest.raises(ValueError):
        EventEnvelope.from_json_line("[]")
    document = json.loads(make_envelope().to_json_line())
    del document["actor"]
    with pytest.raises(ValueError, match="actor"):
        EventEnvelope.from_json_line(json.dumps(document))
    document = json.loads(make_envelope().to_json_line())
    document["payload"] = ["not", "an", "object"]
    with pytest.raises(ValueError, match="payload"):
        EventEnvelope.from_json_line(json.dumps(document))


def test_schema_version_defaults_to_one() -> None:
    assert SCHEMA_VERSION == 1
    assert make_envelope().schema_version == 1
    assert json.loads(make_envelope().to_json_line())["schema_version"] == 1


def test_new_event_id_is_injectable_and_deterministic() -> None:
    moment = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    first = new_event_id(lambda: moment, random.Random(7))
    second = new_event_id(lambda: moment, random.Random(7))
    assert first == second
    assert first.startswith("evt_")
    assert len(first) == len("evt_") + 26
    assert new_event_id(lambda: moment, random.Random(8)) != first
    later = datetime(2026, 11, 6, 12, 0, 1, tzinfo=UTC)
    assert new_event_id(lambda: later, random.Random(7)) != first


def test_new_event_id_accepts_epoch_seconds() -> None:
    assert new_event_id(lambda: 1_760_000_000.0, random.Random(1)) == new_event_id(
        lambda: 1_760_000_000.0, random.Random(1)
    )
