"""What one agent judge sample leaves behind: step records, usage and the artifact manifest.

Records are the transcript the judge.verdict payload references by sha256 (03-grading 8.6,
PLAN.md "Design: the agent_judge component" point 8). Grading reads only the recorded
verdicts, so a regrade never re-runs a judge agent.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .agent_protocol import Verdict

__all__ = [
    "MANIFEST_SCRIPT",
    "MeteredReply",
    "SampleRecord",
    "StepRecord",
    "UsageMeter",
    "manifest_digest",
    "parse_manifest",
    "tree_manifest",
]

#: Lists every entry of the artifact zone as ``<sha256>  ./path`` (``other`` for anything
#: that is neither a file nor a directory). Run with ``cwd`` on the zone.
MANIFEST_SCRIPT = (
    "find . -type f -exec sha256sum -- {} + && "
    "find . ! -type f ! -type d -exec printf 'other  %s\\n' {} +"
)


class MeteredReply(Protocol):
    """One judge reply and what that call cost (``judging.MeteredCompletion``)."""

    @property
    def text(self) -> str: ...

    @property
    def cost_usd(self) -> float: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...


class UsageMeter:
    """Thread-safe running totals of judge model calls (cost as the gateway charged it)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, call: MeteredReply) -> None:
        with self._lock:
            self.cost_usd += call.cost_usd
            self.input_tokens += call.input_tokens
            self.output_tokens += call.output_tokens
            self.calls += 1


@dataclass(frozen=True)
class StepRecord:
    """One model turn and what the sandbox answered."""

    turn: int
    step: int
    """The executed-action number; ``0`` for an invalid reply, which executes nothing."""
    reply: str
    action: Mapping[str, Any] | None = None
    """The parsed action; ``None`` when the reply was not a valid action."""
    error: str | None = None
    observation: str = ""
    """Exactly what the model was shown (truncated, wrapped as untrusted data)."""
    output_sha256: str | None = None
    """sha256 of the full output the sandbox returned, before truncation."""
    output_bytes: int = 0
    injection_hits: tuple[str, ...] = ()
    ignored_actions: int = 0
    """Further actions in the reply that were not executed (the observation says so)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "step": self.step,
            "reply": self.reply,
            "action": None if self.action is None else dict(self.action),
            "error": self.error,
            "observation": self.observation,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "injection_hits": list(self.injection_hits),
            "ignored_actions": self.ignored_actions,
        }


@dataclass(frozen=True)
class SampleRecord:
    """One full agent session: verdict, reason, steps, integrity and usage."""

    vendor: str
    index: int
    session_id: str
    verdict: Verdict
    reason: str
    cited_steps: tuple[int, ...] = ()
    steps: tuple[StepRecord, ...] = ()
    artifact_intact: bool | None = None
    """``False``: /artifact changed during the sample (invalid, counted as an injection
    attempt); ``None``: integrity could not be checked (no sandbox, or the check failed)."""
    artifact_detail: str = ""
    boot_s: float | None = None
    elapsed_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def injection_hits(self) -> tuple[str, ...]:
        found: list[str] = []
        for step in self.steps:
            found.extend(hit for hit in step.injection_hits if hit not in found)
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "index": self.index,
            "session_id": self.session_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "cited_steps": list(self.cited_steps),
            "artifact_intact": self.artifact_intact,
            "artifact_detail": self.artifact_detail,
            "injection_hits": list(self.injection_hits),
            "boot_s": self.boot_s,
            "elapsed_s": self.elapsed_s,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "steps": [step.to_dict() for step in self.steps],
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    """The host-side twin of :data:`MANIFEST_SCRIPT` over the sanitised export."""
    entries: dict[str, str] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(current) / name
            relative = "./" + path.relative_to(root).as_posix()
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                entries[relative] = "other"
            elif path.is_file():
                entries[relative] = _sha256_file(path)
    return entries


def parse_manifest(output: str) -> dict[str, str]:
    """Parse :data:`MANIFEST_SCRIPT` output into ``path -> sha256 | other``."""
    entries: dict[str, str] = {}
    for line in output.splitlines():
        digest, sep, path = line.partition("  ")
        if sep:
            entries[path] = digest
    return entries


def manifest_digest(manifest: Mapping[str, str]) -> str:
    """One sha256 over a manifest, for the transcript."""
    lines = "\n".join(f"{manifest[path]}  {path}" for path in sorted(manifest))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()
