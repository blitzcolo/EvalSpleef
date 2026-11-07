"""Harbor agent runner: ``harbor run`` as an external subprocess (TECH-DESIGN section 9.2).

Runs Harbor tasks using Harbor's native shell integration while redirecting model traffic
to the gateway with the trial key:
    harbor run -p <task> -a <shell> -m <openai-compatible model string pointing at gateway> \\
        -e evalspleef_harbor_bridge.env:SpleefEnvironment --ek provider=<instance>

Harbor is **not installed** in this environment. Tests drive a stub executable placed
on ``PATH``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evalspleef.spi.model import WireProtocol
from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import Sandbox


class HarborRunner(AgentRunner):
    """Runs Harbor as an external subprocess and maps its result to a ShellTurn."""

    name = "harbor"
    version = "0.25.0"
    supported_modes = frozenset({"remote-tools", "in-sandbox"})
    required_protocol: WireProtocol = "chat_completions"

    def __init__(
        self,
        *,
        shell: str = "claude-code",
        task: str = "default",
        executable: str = "harbor",
        extra_args: Sequence[str] = (),
    ) -> None:
        self.shell = shell
        self.task = task
        self.executable = executable
        self.extra_args = list(extra_args)

        self._sandbox: Sandbox | None = None
        self._gateway_url = ""
        self._trial_key = ""
        self._model_alias = ""
        self._instruction = ""
        self._budget: RunnerBudget | None = None
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
        """Initialize runner for a trial."""
        self._sandbox = sandbox
        self._gateway_url = gateway_url.rstrip("/")
        self._trial_key = trial_key
        self._model_alias = model_alias
        self._instruction = instruction
        self._budget = budget
        self._started = True

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        """Run one harbor turn and return the ShellTurn."""
        if not self._started or self._sandbox is None:
            raise RuntimeError("HarborRunner is not started")

        turn_start = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="evalspleef-harbor-") as log_dir:
            # Model string pointing at the gateway in OpenAI-compatible format
            # e.g., openai/<model_alias> or litellm format
            model_string = f"openai/{self._model_alias}"

            env = {
                **os.environ,
                "OPENAI_BASE_URL": f"{self._gateway_url}/v1",
                "OPENAI_API_BASE": f"{self._gateway_url}/v1",
                "OPENAI_API_KEY": self._trial_key,
                "HARBOR_LOG_DIR": log_dir,
            }

            provider_instance = getattr(getattr(self._sandbox, "spec", None), "lease_id", "default")

            # Build command line per TECH-DESIGN section 9.2:
            # harbor run -p <task> -a <shell> -m <model> -e evalspleef_harbor_bridge.env:SpleefEnvironment --ek provider=<instance>
            argv = [
                self.executable,
                "run",
                "-p",
                self.task,
                "-a",
                self.shell,
                "-m",
                model_string,
                "-e",
                "evalspleef_harbor_bridge.env:SpleefEnvironment",
                "--ek",
                f"provider={provider_instance}",
                *self.extra_args,
            ]

            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            input_bytes = message.encode("utf-8") if message else None

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=input_bytes), timeout=max(1, deadline_s)
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
                    duration_s=time.monotonic() - turn_start,
                    trace_ref=None,
                )

            duration = time.monotonic() - turn_start
            stdout_str = stdout_b.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_b.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                return ShellTurn(
                    stop_reason=StopReason.CRASH,
                    final_message=stderr_str or stdout_str or None,
                    steps=1,
                    usage=TokenUsage(),
                    cost_usd=0.0,
                    duration_s=duration,
                    trace_ref=None,
                )

            # Attempt to parse Harbor log output if any was written
            result = self._read_harbor_log(Path(log_dir))
            usage = TokenUsage()
            final_message: str | None = stdout_str or None
            stop = StopReason.COMPLETED
            cost_usd = 0.0
            steps = 1
            trace_ref: str | None = None

            if result is not None:
                usage_raw = result.get("usage")
                if isinstance(usage_raw, dict):
                    usage = TokenUsage(
                        input_tokens=int(usage_raw.get("input_tokens", 0)),
                        output_tokens=int(usage_raw.get("output_tokens", 0)),
                        cached_tokens=int(usage_raw.get("cached_tokens", 0)),
                        reasoning_tokens=int(usage_raw.get("reasoning_tokens", 0)),
                    )
                status = result.get("status")
                if status not in (None, "success", "completed"):
                    stop = StopReason.CRASH
                msg = result.get("message")
                if isinstance(msg, str) and msg:
                    final_message = msg
                if "cost_usd" in result:
                    with contextlib.suppress(ValueError, TypeError):
                        cost_usd = float(result["cost_usd"])
                if "steps" in result:
                    with contextlib.suppress(ValueError, TypeError):
                        steps = int(result["steps"])
                if "trace_ref" in result:
                    trace_ref = str(result["trace_ref"])

            return ShellTurn(
                stop_reason=stop,
                final_message=final_message,
                steps=steps,
                usage=usage,
                cost_usd=cost_usd,
                duration_s=duration,
                trace_ref=trace_ref,
            )

    async def stop(self) -> None:
        """Stop runner."""
        self._started = False
        self._sandbox = None

    @staticmethod
    def _read_harbor_log(log_dir: Path) -> dict[str, Any] | None:
        """Parse the latest json log from harbor log directory, if any."""
        candidates = sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for candidate in reversed(candidates):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return None
