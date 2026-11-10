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

import contextlib
import hashlib
import random
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from evalspleef.core.judge import JudgeClient, JudgePanel
from evalspleef.core.models import CheckKind, RubricItem
from evalspleef.spi.errors import EndpointUnavailable
from evalspleef.spi.gateway import COST_HEADER, PriceSpec

from ..taskpack import load_task_pack
from ..taskpack.models import TaskPack
from ..trial.types import JudgeItemVotes, JudgeVote, RubricItemResult
from .agreement import SEED_TARGET_FAMILY, SeedGoldRow
from .artifacts import WorkspaceExport, sanitise_tree

__all__ = [
    "CHAT_PATH",
    "JUDGE_RETRY_ATTEMPTS",
    "RETRYABLE_STATUSES",
    "GatewayJudgeClient",
    "JudgeRun",
    "JudgeStep",
    "SeedCalibrationResult",
    "evidence_digest",
    "run_seed_calibration",
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


JUDGE_MAX_TOKENS = 1024
JUDGE_TIMEOUT_S = 60.0

#: Retries of a throttled or transiently failing judge call after the first attempt,
#: so one rate-limited sample no longer invalidates the whole trial.
JUDGE_RETRY_ATTEMPTS = 3

#: Upstream statuses a judge call absorbs with backoff (PRD appendix B.6: provider
#: rate limits and transient 5xx are retried, never propagated to the trial).
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

#: Transport failures absorbed the same way: connect/read timeouts and dropped streams.
_RETRYABLE_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)


def _retry_after_seconds(value: str | None) -> float | None:
    """A numeric ``Retry-After`` header value in seconds; anything else is ignored."""
    if value is not None and value.replace(".", "", 1).isdigit():
        return float(value)
    return None


@dataclass(frozen=True)
class MeteredCompletion:
    """One judge reply and what exactly that call cost."""

    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


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
        timeout_s: float = JUDGE_TIMEOUT_S,
        connect_timeout_s: float = 5.0,
        max_tokens: int = JUDGE_MAX_TOKENS,
        transport: httpx.BaseTransport | None = None,
        retry_attempts: int = JUDGE_RETRY_ATTEMPTS,
        retry_base_s: float = 1.0,
        retry_cap_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.model = endpoint_id
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
        self._retry_attempts = retry_attempts
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._random_source = random_source
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0

    @property
    def total_input_tokens(self) -> int:
        return self._input_tokens

    @property
    def total_cost_usd(self) -> float:
        """What the gateway charged for this client's requests (its ``COST_HEADER``)."""
        return self._cost_usd

    @property
    def total_output_tokens(self) -> int:
        return self._output_tokens

    def close(self) -> None:
        self._client.close()

    def complete_metered(self, prompt: str) -> MeteredCompletion:
        """One-shot chat completion; usage tokens accumulate for cost accounting.

        The result carries the cost of this call alone, because the client is shared by
        every trial of a batch and its running totals cannot tell the trials apart.

        A timeout, a dropped connection, a rate limit or a 5xx is a provider failure the
        batch retries (``EndpointUnavailable``), not a reason to invalidate the trial.
        Before that, a 429, 502, 503, 504 or a connect/read timeout is retried inside
        this call with exponential backoff and full jitter (a numeric ``Retry-After``
        overrides the backoff), bounded by ``retry_attempts`` and by the call's own
        timeout budget. Only the successful attempt books cost and token usage.
        """
        return self.chat_metered([{"role": "user", "content": prompt}])

    def chat_metered(self, messages: Sequence[Mapping[str, str]]) -> MeteredCompletion:
        """One chat completion over a whole conversation (the agent judge's turns).

        Same metering and failure rules as :meth:`complete_metered`; prompt caching is
        the route's own setting (``prompt_caching``), so a growing conversation reuses
        its cached prefix without anything sent from here.
        """
        deadline = self._monotonic() + self._timeout_s
        retries = 0
        while True:
            try:
                response = self._client.post(
                    CHAT_PATH,
                    json={
                        "model": self.model,
                        "messages": [dict(message) for message in messages],
                        "max_tokens": self._max_tokens,
                        "temperature": 0.0,
                    },
                )
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                if not self._wait_before_retry(retries, deadline):
                    raise EndpointUnavailable(
                        f"judge {self.model} request failed: {exc!r}"
                    ) from exc
                retries += 1
                continue
            except httpx.TransportError as exc:
                # Not a retryable transport failure (a write or close error): the batch
                # still sees it as EndpointUnavailable, exactly as before the retry loop.
                raise EndpointUnavailable(f"judge {self.model} request failed: {exc!r}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                if response.status_code in RETRYABLE_STATUSES:
                    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                    if self._wait_before_retry(retries, deadline, retry_after):
                        retries += 1
                        continue
                raise EndpointUnavailable(
                    f"judge {self.model} answered {response.status_code}: {response.text[:200]}"
                )
            break
        response.raise_for_status()
        call_cost = 0.0
        # A malformed header charges nothing; the ledger reconciles against bills.
        with contextlib.suppress(ValueError):
            call_cost = float(response.headers.get(COST_HEADER, "0"))
        self._cost_usd += call_cost
        body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"judge gateway returned no choices for {self.model}")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        usage = body.get("usage", {})
        call_input = call_output = 0
        if isinstance(usage, dict):
            call_input = int(usage.get("prompt_tokens", 0) or 0)
            call_output = int(usage.get("completion_tokens", 0) or 0)
        self._input_tokens += call_input
        self._output_tokens += call_output
        text = content if isinstance(content, str) else str(content)
        return MeteredCompletion(text, call_cost, call_input, call_output)

    def _wait_before_retry(
        self, retries: int, deadline: float, retry_after: float | None = None
    ) -> bool:
        """Sleep before retry ``retries`` (0-based); ``False`` when a budget is spent.

        The wait is ``random * base * 2**retries`` (full jitter) unless the upstream
        asked for a specific ``Retry-After``, and is capped at ``retry_cap_s``. The
        call never sleeps past its own timeout budget: when the remaining time cannot
        cover the wait, the caller raises ``EndpointUnavailable`` as it always has.
        """
        if retries >= self._retry_attempts:
            return False
        delay = (
            retry_after
            if retry_after is not None
            else (self._random_source() * self._retry_base_s * (2**retries))
        )
        delay = min(delay, self._retry_cap_s)
        if self._monotonic() + delay > deadline:
            return False
        self._sleep(delay)
        return True

    def complete(self, prompt: str) -> str:
        return self.complete_metered(prompt).text


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
    item_votes: tuple[JudgeItemVotes, ...] = ()
    """Per-judge votes per item, recorded in the ``judge.verdict`` event."""


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
        max_parallel_per_vendor: int = 5,
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
        self._max_parallel_per_vendor = max_parallel_per_vendor
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
        sessions = {
            vendor: _SessionClient(client, session_id) for vendor, client in self._clients.items()
        }
        panel = JudgePanel(
            sessions,
            target_model=self._target_model or pack.task_id,
            target_family=self._target_family,
            k=self._k,
            max_retries=self._max_retries,
            max_parallel_per_vendor=self._max_parallel_per_vendor,
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
        item_votes = tuple(
            JudgeItemVotes(
                item_id=item.item_id,
                votes={
                    vendor: JudgeVote(
                        decision=(
                            "inconclusive" if decision is None else ("pass" if decision else "fail")
                        ),
                        samples=tuple(item.votes_by_vendor.get(vendor, ())),
                    )
                    for vendor, decision in item.vendor_decisions.items()
                },
            )
            for item in result.items
        )
        cost = self._cost(sessions)
        return JudgeRun(
            results=verdicts,
            snapshot_sha256=snapshot[0],
            snapshot_path=snapshot[1],
            cost_usd=cost,
            agreement=result.agreement,
            samples=result.samples,
            manual_queue=result.manual_queue,
            session_id=session_id,
            item_votes=item_votes,
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

    def _cost(self, sessions: Mapping[str, _SessionClient]) -> float:
        """Spend of this run only: an explicit price table wins, else what the gateway charged."""
        total = 0.0
        for vendor, session in sessions.items():
            price = self._prices.get(vendor)
            if price is None:
                total += session.cost_usd
                continue
            total += (
                session.input_tokens * price.input_per_mtok
                + session.output_tokens * price.output_per_mtok
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
        self._lock = threading.Lock()
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def complete(self, prompt: str) -> str:
        tagged = f"judge_session: {self._session_id}\n\n{prompt}"
        metered = getattr(self._inner, "complete_metered", None)
        if metered is not None:
            call: MeteredCompletion = metered(tagged)
            self._add(call.cost_usd, call.input_tokens, call.output_tokens)
            return call.text
        before = self._totals()
        text = self._inner.complete(tagged)
        after = self._totals()
        self._add(after[0] - before[0], after[1] - before[1], after[2] - before[2])
        return text

    def _totals(self) -> tuple[float, int, int]:
        inner = self._inner
        return (
            float(getattr(inner, "total_cost_usd", 0.0)),
            int(getattr(inner, "total_input_tokens", 0)),
            int(getattr(inner, "total_output_tokens", 0)),
        )

    def _add(self, cost_usd: float, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.cost_usd += cost_usd
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens


@dataclass(frozen=True)
class SeedCalibrationResult:
    """The panel's votes over the seed samples and what the runs cost."""

    votes: dict[str, dict[str, dict[str, str]]]
    """sample_id -> item_id -> judge slug -> "pass" / "fail" (inconclusive is missing)."""
    cost_usd: float
    judged_rows: int
    skipped_rows: int
    """Rows naming items a chat panel cannot decide (non-``llm_judge`` kinds)."""


def run_seed_calibration(
    clients: Mapping[str, JudgeClient],
    rows: Sequence[SeedGoldRow],
    *,
    tasks_dir: Path,
    scratch_dir: Path,
    k: int = 5,
    max_retries: int = 2,
    max_parallel_per_vendor: int = 5,
    allow_single_vendor: bool = False,
) -> SeedCalibrationResult:
    """Judge every seed sample with the panel and collect per-judge votes vs the labels.

    Each (task, sample) group gets the same treatment a trial's judge step gets:
    one :class:`JudgeStep` run over the pack's ``llm_judge`` items whose evidence
    digest is built from the sample's answer file(s) (copied into a scratch tree
    and sanitised by the same :func:`sanitise_tree` rules as a workspace export),
    so the judge prompts are the exact shape the batch evaluator sends. Rows
    naming items of any other kind are skipped: chat judges cannot decide them.

    Blocking, like :meth:`JudgeStep.run`: call it from a worker thread.
    """
    groups: dict[str, list[SeedGoldRow]] = {}
    for row in rows:
        groups.setdefault(row.sample_id, []).append(row)
    votes: dict[str, dict[str, dict[str, str]]] = {}
    packs: dict[str, TaskPack] = {}
    total_cost = 0.0
    judged = 0
    skipped = 0
    for sample_id, group in groups.items():
        task_id = group[0].task_id
        if task_id not in packs:
            packs[task_id] = load_task_pack(tasks_dir / task_id)
        pack = packs[task_id]
        decidable = {item.id for item in pack.rubric.items if item.kind is CheckKind.LLM_JUDGE}
        skipped += sum(1 for row in group if row.item_id not in decidable)
        item_ids = {row.item_id for row in group} & decidable
        if not item_ids:
            continue
        sample_scratch = scratch_dir / sample_id
        export = _seed_sample_export(
            pack, group[0].evidence, sample_scratch / "evidence-src", sample_scratch / "export"
        )
        step = JudgeStep(
            clients,
            target_family=SEED_TARGET_FAMILY,
            target_model=sample_id,
            k=k,
            max_retries=max_retries,
            max_parallel_per_vendor=max_parallel_per_vendor,
            allow_single_vendor=allow_single_vendor,
        )
        run = step.run(pack, export, scratch_dir=sample_scratch / "judge")
        total_cost += run.cost_usd
        sample_votes = votes.setdefault(sample_id, {})
        for item in run.item_votes:
            for vendor, vote in item.votes.items():
                if vote.decision in ("pass", "fail"):
                    sample_votes.setdefault(item.item_id, {})[vendor] = vote.decision
        judged += sum(1 for row in group if row.item_id in decidable)
    return SeedCalibrationResult(
        votes=votes, cost_usd=total_cost, judged_rows=judged, skipped_rows=skipped
    )


def _seed_sample_export(
    pack: TaskPack, evidence: str, src_dir: Path, dest_dir: Path
) -> WorkspaceExport:
    """The sanitised one-answer workspace whose digest the sample's judge prompts carry."""
    files = _seed_evidence_files(pack, evidence)
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True)
    for path in files:
        shutil.copyfile(path, src_dir / path.name)
    return sanitise_tree(src_dir, dest_dir, decls=())


def _seed_evidence_files(pack: TaskPack, evidence: str) -> list[Path]:
    """Resolve a seed row's evidence path inside the pack.

    The seed set writes stems (``answers/reference-a``) because the extension
    varies per pack (``.py``/``.patch``/...), so an exact miss falls back to a
    stem glob — the same convention the pack loader documents.
    """
    base = pack.root / evidence
    if base.is_file():
        return [base]
    if base.is_dir():
        return sorted(path for path in base.rglob("*") if path.is_file())
    matches = sorted(path for path in pack.root.glob(f"{evidence}.*") if path.is_file())
    if matches:
        return matches
    raise ValueError(f"seed evidence {evidence!r} not found under {pack.root}")
