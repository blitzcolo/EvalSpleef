"""Judge panel wiring through the model gateway (PRD appendix C.5, invariant I15).

``llm_judge`` rubric items are decided by a :class:`evalspleef.core.judge.JudgePanel`:
every item is sampled k=5 per judge vendor, aggregated by median vote with the C.5
agreement thresholds, with the vendor-count and anti-same-family rules enforced by the
panel itself. Judges reach the models through the gateway chat endpoint only — the
model name is ``ep_<endpoint_id>`` and the request carries an explicit timeout.

Invariant I15 (per-trial judge isolation):

- every sample is a one-shot chat request built solely from this trial's sanitised
  artifact export — prompts never reference another trial's content;
- each run gets its own session id, sent as ``x-spleef-judge-session`` and embedded in
  the prompt header, so judge sessions are auditable per trial and never shared;
- the exact prompt texts are snapshotted under the trial scratch directory and their
  sha256 recorded, so a regrade can prove it judged the same inputs.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from evalspleef.core.judge import JudgeClient, JudgePanel
from evalspleef.core.models import CheckKind, RubricItem
from evalspleef.spi.gateway import PriceSpec

from ..taskpack.models import TaskPack
from ..trial.types import RubricItemResult
from .artifacts import WorkspaceExport

__all__ = [
    "CHAT_PATH",
    "GatewayJudgeClient",
    "JudgeRun",
    "JudgeStep",
    "evidence_digest",
]

#: Gateway chat endpoint relative to the gateway base URL (which ends in /v1).
CHAT_PATH = "/chat/completions"

#: Per-file text budget folded into the evidence digest (prompt inputs stay small).
_DIGEST_FILE_CAP = 16 << 10

#: File extensions whose content may be quoted into the digest (sanitised artifacts only).
_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".json",
        ".md",
        ".txt",
        ".csv",
        ".yaml",
        ".yml",
        ".toml",
        ".sh",
        ".patch",
        ".c",
        ".h",
        ".cpp",
        ".ts",
        ".js",
    }
)


class _PricedJudge(Protocol):
    """The slice of a judge client the cost accounting needs."""

    name: str

    @property
    def total_input_tokens(self) -> int: ...

    @property
    def total_output_tokens(self) -> int: ...


class GatewayJudgeClient:
    """Synchronous :class:`JudgeClient` speaking to one judge endpoint via the gateway.

    Blocking by design: :class:`JudgePanel` is synchronous, and the evaluator runs the
    whole panel in a worker thread so the event loop stays free.
    """

    def __init__(
        self,
        *,
        endpoint_id: str,
        gateway_url: str,
        provider: str,
        family: str,
        api_key: str | None = None,
        name: str | None = None,
        timeout_s: float = 60.0,
        connect_timeout_s: float = 5.0,
        max_tokens: int = 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.model = f"ep_{endpoint_id}"
        self.provider = provider
        self.family = family
        self.name = name or self.model
        self._timeout_s = timeout_s
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=gateway_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            transport=transport,
        )
        self._max_tokens = max_tokens
        self._input_tokens = 0
        self._output_tokens = 0

    @property
    def total_input_tokens(self) -> int:
        return self._input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._output_tokens

    def close(self) -> None:
        self._client.close()

    def complete(self, prompt: str) -> str:
        """One-shot chat completion; usage tokens accumulate for cost accounting."""
        response = self._client.post(
            CHAT_PATH,
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self._max_tokens,
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"judge gateway returned no choices for {self.model}")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        usage = body.get("usage", {})
        if isinstance(usage, dict):
            self._input_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self._output_tokens += int(usage.get("completion_tokens", 0) or 0)
        return content if isinstance(content, str) else str(content)


def evidence_digest(export: WorkspaceExport) -> str:
    """The judge-facing digest built from the sanitised artifacts only.

    Every declared artifact contributes its path, size and sha256; text artifacts also
    contribute a bounded content excerpt. Undeclared files never reach a prompt.
    """
    lines: list[str] = []
    for artifact in export.artifacts:
        lines.append(f"- {artifact.path} ({artifact.size_bytes} bytes, sha256={artifact.sha256})")
        local = artifact.local_path
        if local is None or local.suffix.lower() not in _TEXT_EXTENSIONS or not local.is_file():
            continue
        try:
            text = local.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = text[:_DIGEST_FILE_CAP]
        lines.append(f"  content_excerpt({artifact.path}):")
        lines.append("  " + excerpt.replace("\n", "\n  "))
    return "\n".join(lines)


@dataclass(frozen=True)
class JudgeRun:
    """The panel outcome for one evaluation."""

    results: tuple[RubricItemResult, ...]
    snapshot_sha256: str
    snapshot_path: Path
    cost_usd: float
    agreement: float | None
    samples: int
    manual_queue: tuple[str, ...]
    session_id: str


class JudgeStep:
    """Runs the judge panel over the llm_judge items of one pack.

    ``clients`` maps a vendor label to any object satisfying the core
    :class:`JudgeClient` protocol (``complete`` plus ``name``/``provider``/``family``
    attributes) — production wires :class:`GatewayJudgeClient`, tests wire
    ``core.judge.MockJudge``.
    """

    def __init__(
        self,
        clients: Mapping[str, JudgeClient],
        *,
        target_family: str,
        target_model: str = "",
        k: int = 5,
        max_retries: int = 2,
        prices: Mapping[str, PriceSpec] | None = None,
        allow_single_vendor: bool = False,
    ) -> None:
        if not clients:
            raise ValueError("a judge step needs at least one judge client")
        self._clients = dict(clients)
        self._target_family = target_family
        self._target_model = target_model
        self._k = k
        self._max_retries = max_retries
        self._prices = dict(prices or {})
        self._allow_single_vendor = allow_single_vendor

    def run(
        self,
        pack: TaskPack,
        export: WorkspaceExport,
        *,
        scratch_dir: Path,
    ) -> JudgeRun:
        """Sample, aggregate and snapshot; synchronous (run it in a thread)."""
        items = tuple(item for item in pack.rubric.items if item.kind is CheckKind.LLM_JUDGE)
        digest = evidence_digest(export)
        session_id = uuid.uuid4().hex
        snapshot = self._snapshot(pack, items, digest, session_id, scratch_dir)
        panel = JudgePanel(
            {
                vendor: _SessionClient(client, session_id)
                for vendor, client in self._clients.items()
            },
            target_model=self._target_model or pack.task_id,
            target_family=self._target_family,
            k=self._k,
            max_retries=self._max_retries,
            allow_single_vendor=self._allow_single_vendor,
        )
        result = panel.evaluate_items(items, evidence_digest=digest)
        verdicts = tuple(
            RubricItemResult(
                item.item_id,
                "inconclusive" if item.manual_required else ("pass" if item.passed else "fail"),
                evidence=(),
            )
            for item in result.items
        )
        cost = self._cost()
        return JudgeRun(
            results=verdicts,
            snapshot_sha256=snapshot[0],
            snapshot_path=snapshot[1],
            cost_usd=cost,
            agreement=result.agreement,
            samples=result.samples,
            manual_queue=result.manual_queue,
            session_id=session_id,
        )

    def _snapshot(
        self,
        pack: TaskPack,
        items: tuple[RubricItem, ...],
        digest: str,
        session_id: str,
        scratch_dir: Path,
    ) -> tuple[str, Path]:
        from evalspleef.core.judge import build_judge_prompt

        scratch_dir.mkdir(parents=True, exist_ok=True)
        path = scratch_dir / f"judge-input-{pack.task_id}-{session_id}.txt"
        parts = [f"judge_session: {session_id}", f"task: {pack.task_id}", ""]
        for item in items:
            parts.append(build_judge_prompt(item, evidence_digest=digest))
            parts.append("\n---\n")
        text = "\n".join(parts)
        path.write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), path

    def _cost(self) -> float:
        total = 0.0
        for vendor, client in self._clients.items():
            price = self._prices.get(vendor)
            if price is None:
                continue
            input_tokens = getattr(client, "total_input_tokens", 0)
            output_tokens = getattr(client, "total_output_tokens", 0)
            total += (
                input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
            ) / 1_000_000
        return total


class _SessionClient:
    """Wraps a judge client tagging every request with the trial's judge session."""

    def __init__(self, inner: JudgeClient, session_id: str) -> None:
        self._inner = inner
        self._session_id = session_id
        self.name = getattr(inner, "name", "judge")
        self.provider = getattr(inner, "provider", self.name)
        self.family = getattr(inner, "family", self.name)

    def complete(self, prompt: str) -> str:
        tagged = f"judge_session: {self._session_id}\n\n{prompt}"
        return self._inner.complete(tagged)
