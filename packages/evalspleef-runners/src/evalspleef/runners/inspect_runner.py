"""Inspect AI runner: ``inspect eval`` as an external subprocess (logical track).

TECH-DESIGN section 10 runs Inspect AI as a runner adapter:
``inspect eval --model openai/ep_<id>`` with ``OPENAI_BASE_URL`` pointing at the
gateway. No sandbox is involved.

Inspect AI is **not installed** in this environment; the tests drive a stub
executable placed on ``PATH``. The mapping from the Inspect eval log to a
``ShellTurn`` is therefore **not verified against real Inspect** output.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import Sandbox

from .common import Clock, SystemClock


class InspectRunner(AgentRunner):
    """Runs ``inspect eval`` and maps the eval log to one ``ShellTurn``."""

    name = "inspect"
    version = "0.1.0"
    supported_modes = frozenset({"remote-tools"})
    required_protocol = "chat_completions"

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        executable: str = "inspect",
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._executable = executable
        self._extra_args = extra_args
        self._gateway_url = ""
        self._trial_key = ""
        self._model_alias = ""
        self._started = False

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
        # Inspect runs the whole eval in one subprocess; the sandbox is unused
        # (TECH-DESIGN section 10: the logical track needs no sandbox).
        del sandbox, instruction, budget
        self._gateway_url = gateway_url
        self._trial_key = trial_key
        self._model_alias = model_alias
        self._started = True

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        if not self._started:
            raise RuntimeError("runner is not started")
        turn_start = self._clock.now()
        with tempfile.TemporaryDirectory(prefix="evalspleef-inspect-") as log_dir:
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "OPENAI_BASE_URL": self._gateway_url,
                "OPENAI_API_KEY": self._trial_key,
                "INSPECT_LOG_DIR": log_dir,
            }
            argv = [
                self._executable,
                "eval",
                *self._extra_args,
                "--model",
                f"openai/{self._model_alias}",
            ]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=max(1, deadline_s)
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ShellTurn(
                    stop_reason=StopReason.TIMEOUT,
                    final_message=None,
                    steps=1,
                    usage=TokenUsage(),
                    cost_usd=0.0,
                    duration_s=self._clock.now() - turn_start,
                    trace_ref=None,
                )
            duration = self._clock.now() - turn_start
            if proc.returncode != 0:
                return ShellTurn(
                    stop_reason=StopReason.CRASH,
                    final_message=stderr_b.decode("utf-8", errors="replace").strip() or None,
                    steps=1,
                    usage=TokenUsage(),
                    cost_usd=0.0,
                    duration_s=duration,
                    trace_ref=None,
                )
            result = self._read_log(Path(log_dir))
            usage = TokenUsage()
            final_message: str | None = stdout_b.decode("utf-8", errors="replace").strip() or None
            stop = StopReason.COMPLETED
            if result is not None:
                usage_raw = result.get("usage")
                if isinstance(usage_raw, dict):
                    usage = TokenUsage(
                        input_tokens=int(usage_raw.get("input_tokens", 0)),
                        output_tokens=int(usage_raw.get("output_tokens", 0)),
                        cached_tokens=int(usage_raw.get("cached_tokens", 0)),
                        reasoning_tokens=int(usage_raw.get("reasoning_tokens", 0)),
                    )
                if result.get("status") not in (None, "success"):
                    stop = StopReason.CRASH
                message_raw = result.get("message")
                if isinstance(message_raw, str) and message_raw:
                    final_message = message_raw
            return ShellTurn(
                stop_reason=stop,
                final_message=final_message,
                steps=1,
                usage=usage,
                cost_usd=0.0,
                duration_s=duration,
                trace_ref=None,
            )

    async def stop(self) -> None:
        """Nothing to kill: each ``send`` runs a one-shot subprocess."""
        self._started = False

    @staticmethod
    def _read_log(log_dir: Path) -> dict[str, object] | None:
        """Parse the newest ``*.json`` eval log in the log directory, if any."""
        candidates = sorted(log_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return None
