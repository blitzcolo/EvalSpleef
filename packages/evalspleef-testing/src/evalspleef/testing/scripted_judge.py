"""A deterministic multi-turn judge model for agent judge tests.

``ScriptedJudgeModel`` stands in for a gateway chat client (``chat_metered(messages)``):
every reply comes from a script that sees the whole conversation, so a test can answer
with fixed JSON actions or decide from what the sandbox returned. Each call is charged
a fixed cost and token count, so cost accounting is checkable.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Script", "ScriptedJudgeModel", "ScriptedReply", "turns"]

Script = Callable[[Sequence[Mapping[str, str]]], str]


@dataclass(frozen=True)
class ScriptedReply:
    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


def turns(*replies: str | Mapping[str, Any]) -> Script:
    """Answer the n-th turn of every conversation with ``replies[n]`` (the last repeats).

    The turn is the number of assistant messages already in the conversation, so every
    sample, which starts a new conversation, replays the same script.
    """
    if not replies:
        raise ValueError("turns() needs at least one reply")
    texts = [reply if isinstance(reply, str) else json.dumps(reply) for reply in replies]

    def script(messages: Sequence[Mapping[str, str]]) -> str:
        turn = sum(1 for message in messages if message.get("role") == "assistant")
        return texts[min(turn, len(texts) - 1)]

    return script


@dataclass
class ScriptedJudgeModel:
    script: Script
    name: str = "scripted-judge"
    provider: str = "scripted"
    family: str = "scripted-family"
    cost_per_call_usd: float = 0.001
    input_tokens_per_call: int = 100
    output_tokens_per_call: int = 10
    conversations: list[tuple[dict[str, str], ...]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def chat_metered(self, messages: Sequence[Mapping[str, str]]) -> ScriptedReply:
        """Record the conversation and answer from the script."""
        with self._lock:
            self.conversations.append(tuple(dict(message) for message in messages))
        return ScriptedReply(
            self.script(messages),
            self.cost_per_call_usd,
            self.input_tokens_per_call,
            self.output_tokens_per_call,
        )
