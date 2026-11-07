"""Scripted agent runner: replays shell commands and file writes as "agent rounds".

Each call to ``send`` consumes the next ``ScriptedRound`` and executes it against the sandbox:
file writes land in ``/workspace`` and commands run with ``/workspace`` as cwd. Every command
and every file write counts as one step. The returned ``ShellTurn`` carries the round's
configured stop reason, final message, token usage, cost and duration.

An empty script models the empty agent: it completes immediately with zero steps, zero usage
and zero cost. A list of per-round scripts models a feedback loop where each round reacts to
the message that opened it; an exhausted script never repeats its side effects.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from evalspleef.spi.caps import ExecutionMode
from evalspleef.spi.model import WireProtocol
from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import ExecResult, Sandbox

Command = str | list[str]
FileContent = str | bytes

WORKSPACE = "/workspace"


@dataclass(frozen=True)
class ScriptedRound:
    """What the scripted agent does in one round, and what it reports about it."""

    commands: tuple[Command, ...] = ()
    files: Mapping[str, FileContent] = field(default_factory=dict)
    stop_reason: StopReason = StopReason.COMPLETED
    final_message: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    duration_s: float = 0.0


class ScriptedRunner(AgentRunner):
    """An ``AgentRunner`` that replays pre-written rounds instead of calling a model."""

    name = "scripted"
    version = "0.1.0"
    supported_modes: frozenset[ExecutionMode] = frozenset({"remote-tools", "in-sandbox"})
    required_protocol: WireProtocol = "chat_completions"

    def __init__(self, rounds: Sequence[ScriptedRound] | None = None) -> None:
        self._rounds = list(rounds or [])
        self._sandbox: Sandbox | None = None
        self._stopped = False
        self._received: list[str] = []
        self._exec_results: list[ExecResult] = []
        self._instruction: str | None = None
        self._gateway_url: str | None = None
        self._trial_key: str | None = None
        self._model_alias: str | None = None
        self._budget: RunnerBudget | None = None

    @staticmethod
    def noop() -> ScriptedRunner:
        """The empty agent: completes immediately with zero steps and zero usage."""
        return ScriptedRunner([])

    async def start(
        self,
        sandbox: Sandbox,
        *,
        gateway_url: str,
        trial_key: str,
        model_alias: str,
        instruction: str,
        budget: RunnerBudget,
    ) -> None:
        self._sandbox = sandbox
        self._gateway_url = gateway_url
        self._trial_key = trial_key
        self._model_alias = model_alias
        self._instruction = instruction
        self._budget = budget
        self._stopped = False

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        if self._sandbox is None:
            raise RuntimeError("runner is not started; call start() first")
        if self._stopped:
            raise RuntimeError("runner is stopped")
        del deadline_s  # the script is instant; time is reported by the round itself
        index = len(self._received)
        self._received.append(message)
        if index >= len(self._rounds):
            # No script left: the agent has nothing more to do and repeats no side effects.
            return ShellTurn(
                stop_reason=StopReason.COMPLETED,
                final_message=None,
                steps=0,
                usage=TokenUsage(),
                cost_usd=0.0,
                duration_s=0.0,
            )
        script = self._rounds[index]
        steps = await self._run_round(script)
        return ShellTurn(
            stop_reason=script.stop_reason,
            final_message=script.final_message,
            steps=steps,
            usage=script.usage,
            cost_usd=script.cost_usd,
            duration_s=script.duration_s,
        )

    async def stop(self) -> None:
        """Idempotent; there is no agent process group to kill."""
        self._stopped = True

    @property
    def received_messages(self) -> list[str]:
        """Every message sent to the runner so far, in order."""
        return list(self._received)

    @property
    def exec_results(self) -> list[ExecResult]:
        """Every ``ExecResult`` produced by the scripted commands, in order."""
        return list(self._exec_results)

    @property
    def instruction(self) -> str | None:
        return self._instruction

    @property
    def gateway_url(self) -> str | None:
        return self._gateway_url

    @property
    def trial_key(self) -> str | None:
        return self._trial_key

    @property
    def model_alias(self) -> str | None:
        return self._model_alias

    @property
    def budget(self) -> RunnerBudget | None:
        return self._budget

    async def _run_round(self, script: ScriptedRound) -> int:
        assert self._sandbox is not None
        steps = 0
        for relative, content in script.files.items():
            remote = relative if relative.startswith("/") else f"{WORKSPACE}/{relative}"
            await self._upload(content, remote)
            steps += 1
        for command in script.commands:
            result = await self._sandbox.exec(command, cwd=WORKSPACE)
            self._exec_results.append(result)
            steps += 1
            if result.timed_out:
                break
        return steps

    async def _upload(self, content: FileContent, remote: str) -> None:
        assert self._sandbox is not None
        data = content.encode() if isinstance(content, str) else content
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            staging = Path(handle.name)
        try:
            await self._sandbox.upload(staging, remote)
        finally:
            await asyncio.to_thread(staging.unlink, missing_ok=True)
