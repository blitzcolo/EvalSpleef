"""Tests for the shared runner helpers in ``common.py``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evalspleef.runners.common import (
    REDACTED,
    Heartbeat,
    LoopDetector,
    ToolArgumentsError,
    TranscriptWriter,
    parse_tool_arguments,
    redact_obj,
    redact_text,
    tool_call_key,
)
from evalspleef.testing.clock import FakeClock


def test_redact_text_sk_pattern() -> None:
    text = "key=sk-abcdef1234567890XYZ other"
    assert redact_text(text) == f"key={REDACTED} other"
    # Short or non-key "sk-" words are left alone.
    assert redact_text("ask-sk-y") == "ask-sk-y"


def test_redact_text_extra_secret() -> None:
    text = "Authorization: Bearer trial-key-0123456789"
    assert (
        redact_text(text, secrets=("trial-key-0123456789",)) == f"Authorization: Bearer {REDACTED}"
    )


def test_redact_obj_nested() -> None:
    value = {
        "messages": [
            {"role": "user", "content": "use key sk-abcdef1234567890 please"},
            {"role": "tool", "content": ["plain", 3]},
        ]
    }
    redacted = redact_obj(value)
    assert redacted["messages"][0]["content"] == f"use key {REDACTED} please"
    assert redacted["messages"][1]["content"] == ["plain", 3]


def test_parse_tool_arguments_ok() -> None:
    assert parse_tool_arguments('{"command": "ls"}') == {"command": "ls"}


def test_parse_tool_arguments_invalid_json() -> None:
    with pytest.raises(ToolArgumentsError):
        parse_tool_arguments("{not json")


def test_parse_tool_arguments_not_an_object() -> None:
    with pytest.raises(ToolArgumentsError):
        parse_tool_arguments('["a"]')


def test_tool_call_key_is_stable() -> None:
    assert tool_call_key("bash", {"b": 1, "a": 2}) == tool_call_key("bash", {"a": 2, "b": 1})
    assert tool_call_key("bash", {"a": 1}) != tool_call_key("read", {"a": 1})


def test_loop_detector_threshold() -> None:
    detector = LoopDetector(threshold=3)
    assert not detector.observe("a")
    assert not detector.observe("a")
    assert detector.observe("a")
    assert not detector.observe("b")
    assert not detector.observe("b")
    assert detector.observe("b")  # third consecutive "b"
    detector.reset()
    assert not detector.observe("b")


async def test_heartbeat_beats_on_fake_clock() -> None:
    clock = FakeClock()
    beats: list[float] = []
    heartbeat = Heartbeat(clock, 15.0, lambda: beats.append(clock.now()))
    async with heartbeat:
        for _ in range(3):
            await asyncio.sleep(0)
        assert clock.pending_sleepers == 1
        clock.advance(15.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert beats == [15.0]
        clock.advance(30.0)
        for _ in range(4):
            await asyncio.sleep(0)
        assert beats == [15.0, 45.0]
    for _ in range(2):
        await asyncio.sleep(0)
    assert clock.pending_sleepers == 0


async def test_transcript_writer_redacts(tmp_path: Path) -> None:
    writer = TranscriptWriter(tmp_path / "trace.jsonl", secrets=("trial-key-0123456789",))
    await writer.record({"type": "message", "content": "key is trial-key-0123456789 ok"})
    await writer.record({"type": "message", "content": "and sk-abcdef1234567890 too"})
    writer.close()

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    events = [json.loads(line) for line in lines]
    assert events[0]["content"] == f"key is {REDACTED} ok"
    assert events[1]["content"] == f"and {REDACTED} too"
    assert "trial-key-0123456789" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
