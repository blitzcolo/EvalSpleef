"""Event log envelope and event types (PRD appendix B.8, PRD FR-FLOW-04).

The event log is append-only and authoritative; every state change must be
accompanied by an event (AGENTS.md architecture rule 6, docs/02-eval-flow.md
section 11).  This module only models the envelope and its canonical JSON
encoding; it deliberately knows nothing about storage.

Canonical encoding: one JSON object per line, object keys sorted, no ASCII
escaping (UTF-8), no insignificant whitespace.  Two envelopes that compare
equal therefore produce byte-identical lines.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "EventEnvelope",
    "EventType",
    "UnknownEventType",
    "new_event_id",
]

#: Envelope schema version carried by every event (PRD appendix B.8).
SCHEMA_VERSION = 1

#: Crockford base32 alphabet used by ULID-style identifiers.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class EventType(StrEnum):
    """Every event type the platform may append.

    The first block is required by PRD appendix B.8; the second block is the
    version-3 addition of PRD FR-FLOW-04.
    """

    # PRD appendix B.8 required event types.
    TRIAL_CREATED = "trial.created"
    TRIAL_LEASED = "trial.leased"
    SANDBOX_PROVISIONED = "sandbox.provisioned"
    CANARY_INJECTED = "canary.injected"
    PREFLIGHT_RESULT = "preflight.result"
    TRIAL_STARTED = "trial.started"
    ROUND_STARTED = "round.started"
    AGENT_STEP = "agent.step"
    AGENT_STOPPED = "agent.stopped"
    OBSERVE_STARTED = "observe.started"
    HARDGATE_RESULT = "hardgate.result"
    EVIDENCE_COLLECTED = "evidence.collected"
    DEFECT_UPDATED = "defect.updated"
    FEEDBACK_DECIDED = "feedback.decided"
    FEEDBACK_LEAKCHECK = "feedback.leakcheck"
    FEEDBACK_SENT = "feedback.sent"
    BUDGET_THRESHOLD = "budget.threshold"
    VERIFY_STARTED = "verify.started"
    HIDDENTEST_RESULT = "hiddentest.result"
    JUDGE_VERDICT = "judge.verdict"
    GRADE_COMPUTED = "grade.computed"
    TRIAL_SCORED = "trial.scored"
    TRIAL_INVALIDATED = "trial.invalidated"
    TEARDOWN_STARTED = "teardown.started"
    TEARDOWN_COMPLETED = "teardown.completed"
    CANARY_SCANNED = "canary.scanned"
    INCIDENT_OPENED = "incident.opened"

    # PRD FR-FLOW-04 version-3 event types.
    ENDPOINT_REGISTERED = "endpoint.registered"
    ENDPOINT_PROBE_RESULT = "endpoint.probe.result"
    ENDPOINT_WARM = "endpoint.warm"
    ENDPOINT_COLD_START = "endpoint.cold_start"
    ENDPOINT_UNAVAILABLE = "endpoint.unavailable"
    ENDPOINT_RETIRED = "endpoint.retired"
    PROVIDER_CAPABILITY_REJECT = "provider.capability.reject"
    HOST_DRAINED = "host.drained"
    SANDBOX_ORPHAN_REAPED = "sandbox.orphan.reaped"


class UnknownEventType(ValueError):
    """Raised when an envelope carries a type outside :class:`EventType`."""


_REQUIRED_KEYS = ("event_id", "schema_version", "ts", "batch_id", "type", "actor")


@dataclass(frozen=True)
class EventEnvelope:
    """One append-only event record (PRD appendix B.8).

    The field order matches the documented envelope; ``schema_version``,
    ``trial_id`` and ``payload`` have defaults so callers can omit the parts
    that do not apply to batch-level events.
    """

    event_id: str
    ts: str
    batch_id: str
    type: EventType
    actor: str
    schema_version: int = SCHEMA_VERSION
    trial_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, EventType):
            raise UnknownEventType(f"unknown event type: {self.type!r}")
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError(f"schema_version must be a positive integer: {self.schema_version!r}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready representation of the envelope."""
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "ts": self.ts,
            "batch_id": self.batch_id,
            "trial_id": self.trial_id,
            "type": str(self.type),
            "actor": self.actor,
            "payload": dict(self.payload),
        }

    def to_json_line(self) -> str:
        """Canonical single-line JSON (sorted keys, UTF-8, no extra whitespace)."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> EventEnvelope:
        """Parse a line produced by :meth:`to_json_line`.

        Raises:
            ValueError: the line is not a JSON object or lacks a required field.
            UnknownEventType: the event type is not a member of :class:`EventType`.
        """
        raw: Any = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("event envelope must be a JSON object")
        missing = [key for key in _REQUIRED_KEYS if key not in raw]
        if missing:
            raise ValueError(f"event envelope missing fields: {', '.join(missing)}")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("event envelope payload must be a JSON object")
        trial_id = raw.get("trial_id")
        if trial_id is not None and not isinstance(trial_id, str):
            raise ValueError("event envelope trial_id must be a string or null")
        try:
            event_type = EventType(str(raw["type"]))
        except ValueError as exc:
            raise UnknownEventType(f"unknown event type: {raw['type']!r}") from exc
        return cls(
            event_id=str(raw["event_id"]),
            schema_version=int(raw["schema_version"]),
            ts=str(raw["ts"]),
            batch_id=str(raw["batch_id"]),
            trial_id=trial_id,
            type=event_type,
            actor=str(raw["actor"]),
            payload=payload,
        )


def _encode_base32(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters, most significant first."""
    chars: list[str] = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_event_id(
    clock: Callable[[], datetime | float] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Return a ULID-style event identifier: ``evt_`` + 26 base32 characters.

    ``clock`` and ``rng`` are injectable so tests and replay tooling produce
    deterministic identifiers: the first 48 bits are the clock in milliseconds,
    the remaining 80 bits come from ``rng.getrandbits(80)``.

    Args:
        clock: Returns the current time as :class:`datetime` or epoch seconds.
            Defaults to :func:`datetime.now` in UTC.
        rng: Random source used for the entropy suffix. Defaults to
            :class:`random.SystemRandom`.
    """
    now: datetime | float = clock() if clock is not None else datetime.now(UTC)
    if isinstance(now, datetime):
        moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        timestamp_ms = int(moment.timestamp() * 1000)
    else:
        timestamp_ms = int(float(now) * 1000)
    entropy = (rng if rng is not None else random.SystemRandom()).getrandbits(80)
    return f"evt_{_encode_base32(timestamp_ms & 0xFFFFFFFFFFFF, 10)}{_encode_base32(entropy, 16)}"
