"""One agent judge sample inside its own judge sandbox (PLAN.md "Design: the agent_judge
component", points 3-6).

The sandbox arrives fresh from the opener. The session lays out the three zones
(``/artifact`` read-only, ``/work`` a writable copy, ``/tmp`` scratch), runs the action
loop with the model through the gateway, then hashes ``/artifact`` inside the sandbox and
compares it with the host-side hash of the export: a mismatch makes the sample invalid and
counts as an injection attempt (03-grading 8.6). Every failure ends the sample as
``inconclusive`` with a reason (03-grading 11.8), except a judge provider outage, which
propagates like it does for chat judges.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, assert_never

import httpx

from evalspleef.core.models import RubricItem
from evalspleef.spi.errors import EndpointUnavailable, SpleefError
from evalspleef.spi.sandbox import ExecResult, Sandbox

from .agent_prompt import ARTIFACT_DIR, AgentJudgeLimits, system_prompt
from .agent_protocol import (
    WORK_DIR,
    Action,
    ActionError,
    ListDir,
    ReadFile,
    RunCommand,
    SubmitVerdict,
    Verdict,
    WriteFile,
    cap_output,
    injection_hits,
    parse_reply,
    wrap_untrusted,
)
from .agent_records import (
    MANIFEST_SCRIPT,
    MeteredReply,
    SampleRecord,
    StepRecord,
    UsageMeter,
    manifest_digest,
    parse_manifest,
)

__all__ = ["ARTIFACT_DIR", "AgentJudgeLimits", "AgentSession", "ChatJudge", "SampleSpec"]

#: What the sandbox may return per command before the transcript hashes it; the model
#: sees at most ``output_cap_bytes`` of it.
_FULL_OUTPUT_BYTES = 1 << 20
_SETUP_TIMEOUT_S = 120.0
_REPLY_RECORD_CHARS = 16 << 10


class ChatJudge(Protocol):
    """A judge model reachable for multi-turn chat (``GatewayJudgeClient``)."""

    name: str
    provider: str
    family: str

    def chat_metered(self, messages: Sequence[Mapping[str, str]]) -> MeteredReply: ...


@dataclass(frozen=True)
class SampleSpec:
    """Which sample this is: the item it decides and its place in the panel."""

    item: RubricItem
    vendor: str
    index: int
    session_id: str


class _SampleFailed(Exception):
    """The sample cannot go on; its message is the inconclusive reason."""


def _render(result: ExecResult) -> bytes:
    flags = "".join(
        note
        for on, note in ((result.timed_out, " (timed out)"), (result.truncated, " (cut)"))
        if on
    )
    head = f"exit_code: {result.exit_code}{flags}\n--- stdout ---\n".encode()
    return head + result.stdout + b"\n--- stderr ---\n" + result.stderr


class AgentSession:
    """Runs samples for one judge model; the sandbox lifecycle belongs to the caller."""

    def __init__(self, model: ChatJudge, meter: UsageMeter, limits: AgentJudgeLimits) -> None:
        self.model = model
        self._meter = meter
        self._limits = limits

    async def run(
        self, sandbox: Sandbox, spec: SampleSpec, expected: Mapping[str, str]
    ) -> SampleRecord:
        """Prepare the zones, run the loop, check ``/artifact`` against ``expected``."""
        started = time.monotonic()
        usage = UsageMeter()
        steps: list[StepRecord] = []
        cited: tuple[int, ...] = ()
        try:
            async with asyncio.timeout(self._limits.wall_clock_s):
                await self._prepare(sandbox)
                verdict, reason, cited = await self._loop(sandbox, spec, steps, usage)
        except TimeoutError:
            verdict, reason = "inconclusive", f"wall clock limit {self._limits.wall_clock_s:g} s"
        except EndpointUnavailable:
            raise
        except (SpleefError, _SampleFailed) as exc:
            verdict, reason = "inconclusive", f"{type(exc).__name__}: {exc}"
        intact, detail = await self._check_artifact(sandbox, expected)
        if intact is False:
            verdict, reason = "inconclusive", f"invalid sample: /artifact changed ({detail})"
        elif intact is None:
            # Fail closed: a verdict on an artifact nobody could verify is not kept.
            verdict, reason = "inconclusive", f"integrity unverifiable: {detail}"
        return SampleRecord(
            vendor=spec.vendor,
            index=spec.index,
            session_id=spec.session_id,
            verdict=verdict,
            reason=reason,
            cited_steps=cited,
            steps=tuple(steps),
            artifact_intact=intact,
            artifact_detail=detail,
            elapsed_s=round(time.monotonic() - started, 3),
            cost_usd=usage.cost_usd,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    async def _exec(self, sandbox: Sandbox, cmd: str | list[str], cwd: str) -> ExecResult:
        return await sandbox.exec(
            cmd,
            cwd=cwd,
            user=sandbox.spec.run_as_user,
            timeout_s=self._limits.command_timeout_s,
            max_output_bytes=_FULL_OUTPUT_BYTES,
        )

    async def _prepare(self, sandbox: Sandbox) -> None:
        # Overlay by explicit copy, as the judge user: no overlayfs, no privilege needed.
        # Entries are copied one by one: copying the directory itself would try to stamp
        # its times and mode onto the root-owned mount point. The copy keeps the
        # artifact's modes, so write bits are added back in /work.
        script = (
            f"find {ARTIFACT_DIR} -mindepth 1 -maxdepth 1 -exec cp -a {{}} {WORK_DIR}/ \\; && "
            f"find {WORK_DIR} -mindepth 1 ! -type l -exec chmod u+w {{}} +"
        )
        result = await sandbox.exec(
            ["sh", "-c", script],
            cwd="/",
            user=sandbox.spec.run_as_user,
            timeout_s=_SETUP_TIMEOUT_S,
            max_output_bytes=8 << 10,
        )
        if result.exit_code != 0:
            raise _SampleFailed(f"cannot fill {WORK_DIR}: {result.stderr[-500:]!r}")

    def _call(self, messages: Sequence[Mapping[str, str]], usage: UsageMeter) -> str:
        # Runs in a worker thread. Metering happens here, so a call the wall clock
        # abandoned is still charged to the run.
        call = self.model.chat_metered(messages)
        self._meter.add(call)
        usage.add(call)
        return call.text

    async def _ask(self, messages: list[dict[str, str]], usage: UsageMeter) -> str:
        try:
            return await asyncio.to_thread(self._call, tuple(messages), usage)
        except (httpx.HTTPStatusError, ValueError) as exc:
            raise _SampleFailed(f"judge model call failed: {exc}") from exc

    async def _loop(
        self,
        sandbox: Sandbox,
        spec: SampleSpec,
        steps: list[StepRecord],
        usage: UsageMeter,
    ) -> tuple[Verdict, str, tuple[int, ...]]:
        """Executed actions count toward ``max_steps``; invalid replies have their own cap."""
        limit, tolerated = self._limits.max_steps, self._limits.max_invalid_replies
        messages = [
            {"role": "system", "content": system_prompt(spec.item, self._limits)},
            {
                "role": "user",
                "content": f"judge_session: {spec.session_id}\nsample: {spec.vendor}#"
                f"{spec.index}\nStep 1 of {limit}: answer with your first action.",
            },
        ]
        executed = invalid = 0
        while executed < limit:
            turn = executed + invalid + 1
            reply = await self._ask(messages, usage)
            messages.append({"role": "assistant", "content": reply})
            step = executed + 1
            try:
                parsed = parse_reply(reply, step=step)
            except ActionError as exc:
                invalid += 1
                record = StepRecord(turn, 0, reply[:_REPLY_RECORD_CHARS], error=str(exc))
                steps.append(record)
                if invalid > tolerated:
                    return "inconclusive", f"protocol: {invalid} invalid replies", ()
                retry = (
                    f"Invalid answer: {exc}. Step {step} of {limit} still open: send one action."
                )
                messages.append({"role": "user", "content": retry})
                continue
            executed = step
            action = parsed.action
            described = {"kind": type(action).__name__, **dataclasses.asdict(action)}
            if isinstance(action, SubmitVerdict):
                steps.append(StepRecord(turn, step, reply[:_REPLY_RECORD_CHARS], action=described))
                return action.verdict, action.reason, action.steps
            output = _render(await self._perform(sandbox, action))
            shown, _cut = cap_output(output, self._limits.output_cap_bytes)
            observation = "\n".join(filter(None, (wrap_untrusted(step, shown), parsed.notice)))
            steps.append(
                StepRecord(
                    turn,
                    step,
                    reply[:_REPLY_RECORD_CHARS],
                    action=described,
                    observation=observation,
                    output_sha256=hashlib.sha256(output).hexdigest(),
                    output_bytes=len(output),
                    injection_hits=injection_hits(output.decode("utf-8", errors="replace")),
                    ignored_actions=parsed.ignored_actions,
                )
            )
            following = f"Step {step + 1} of {limit} next: answer with exactly one JSON action."
            messages.append({"role": "user", "content": f"{observation}\n{following}"})
        return "inconclusive", f"step limit {limit} reached without a verdict", ()

    async def _perform(self, sandbox: Sandbox, action: Action) -> ExecResult:
        match action:
            case RunCommand(cmd=cmd):
                return await self._exec(sandbox, cmd, WORK_DIR)
            case ReadFile(path=path):
                return await self._exec(sandbox, ["cat", "--", path], WORK_DIR)
            case ListDir(path=path):
                return await self._exec(sandbox, ["ls", "-la", "--", path], WORK_DIR)
            case WriteFile(path=path, content=content):
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                script = 'mkdir -p "$(dirname "$2")" && printf %s "$1" | base64 -d > "$2"'
                return await self._exec(sandbox, ["sh", "-c", script, "sh", encoded, path], "/")
            case SubmitVerdict():
                raise AssertionError("submit_verdict ends the loop before it is performed")
            case unreachable:
                assert_never(unreachable)

    async def _check_artifact(
        self, sandbox: Sandbox, expected: Mapping[str, str]
    ) -> tuple[bool | None, str]:
        try:
            result = await sandbox.exec(
                ["sh", "-c", MANIFEST_SCRIPT],
                cwd=ARTIFACT_DIR,
                timeout_s=_SETUP_TIMEOUT_S,
                max_output_bytes=16 << 20,
            )
        except SpleefError as exc:
            return None, f"check failed: {exc}"
        if result.exit_code != 0 or result.truncated:
            return None, f"check exited {result.exit_code} (truncated={result.truncated})"
        actual = parse_manifest(result.stdout.decode("utf-8", errors="replace"))
        if actual == expected:
            return True, f"sha256 {manifest_digest(actual)}"
        changed = sorted(set(actual) ^ set(expected)) + sorted(
            path for path in set(actual) & set(expected) if actual[path] != expected[path]
        )
        return False, f"{len(changed)} paths differ: {', '.join(changed[:5])}"
