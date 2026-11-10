"""Native shell runners (``in-sandbox`` mode): Claude Code, Codex CLI, OpenCode.

The CLI is preinstalled in the sandbox template and runs inside it; the only model
egress is the gateway, authenticated with the per-trial key (TECH-DESIGN section
8.3). The runner uploads a generated configuration, starts the CLI with a
whitelisted environment (``env -i`` style: PATH, HOME and only the variables that
CLI needs) and drives one turn per ``send``.

Web search, telemetry and auto-update are disabled through each CLI's documented
settings. The exact setting names were verified against the vendors' current
documentation (source URLs in README.md) but are **not verified against the real
CLIs** — no sandbox image with the real tools exists in this repository.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
import uuid
from pathlib import Path

from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import Sandbox

from . import tracing
from .common import Clock, SystemClock

WORKSPACE = "/workspace"

# Output contract between the runner and the in-sandbox CLI: the last line of a
# turn's stdout may carry ``SPLEEF_RESULT {json}`` with keys ``stop_reason`` and
# ``message``. Without the marker the runner maps exit code 0 to ``completed``
# with the raw stdout as the final message, and a non-zero exit to ``crash``.
RESULT_MARKER = "SPLEEF_RESULT "


class NativeShellRunner(AgentRunner):
    """Base class for in-sandbox native shells."""

    supported_modes = frozenset({"in-sandbox"})
    version = "0.1.0"

    #: Executable name inside the sandbox template.
    cli: str = ""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        cli: str | None = None,
        serve_args: tuple[str, ...] = ("--serve",),
        turn_args: tuple[str, ...] = ("--turn",),
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        if cli is not None:
            self.cli = cli
        if not self.cli:
            raise ValueError("cli executable is not set")
        self._serve_args = serve_args
        self._turn_args = turn_args
        self._sandbox: Sandbox | None = None
        self._gateway_url = ""
        self._trial_key = ""
        self._model_alias = ""
        self._home = ""
        self._pgid: int | None = None
        self._started = False

    # -- per-CLI contract ---------------------------------------------------------

    def cli_env(self) -> dict[str, str]:
        """CLI-specific environment variables; merged into the whitelist."""
        raise NotImplementedError

    def config_files(self) -> dict[str, str]:
        """Remote path -> content for the generated configuration files."""
        raise NotImplementedError

    # -- AgentRunner -----------------------------------------------------------------

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
        del instruction, budget  # the CLI reads the task from its own session
        self._sandbox = sandbox
        self._gateway_url = gateway_url
        self._trial_key = trial_key
        self._model_alias = model_alias
        self._home = f"{WORKSPACE}/.spleef-native/{self.name}-home"
        for remote, content in self.config_files().items():
            await self._upload_text(remote, content)
        serve_out = f".spleef-native/{self.name}-home/serve.log"
        # setsid makes the CLI its own process group leader where available
        # (Linux templates), so stop() can kill the whole group; elsewhere the
        # fallback in stop() kills the recorded pid directly.
        launcher = 'if command -v setsid >/dev/null 2>&1; then exec setsid "$@"; else exec "$@"; fi'
        command = (
            f"nohup sh -c {shlex.quote(launcher)} spleef-launch "
            f"{shlex.join([self.cli, *self._serve_args])} "
            f">{shlex.quote(serve_out)} 2>&1 </dev/null & echo $!"
        )
        launched = await sandbox.exec(
            command,
            cwd=WORKSPACE,
            user="agent",
            env=self.whitelist_env(),
            timeout_s=30,
        )
        if launched.exit_code != 0:
            raise RuntimeError(
                f"failed to launch {self.cli}: {launched.stderr.decode('utf-8', errors='replace')}"
            )
        try:
            self._pgid = int(launched.stdout.decode("utf-8", errors="replace").strip())
        except ValueError as exc:
            raise RuntimeError(f"failed to parse {self.cli} pid from: {launched.stdout!r}") from exc
        self._started = True

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        if not self._started or self._sandbox is None:
            raise RuntimeError("runner is not started")
        turn_start = self._clock.now()
        # The message file path is passed to the CLI as an exec argument; sandboxes
        # that remap the workspace only translate cwd, so the argument stays
        # relative to /workspace (the turn exec's cwd).
        with tracing.tracer().start_as_current_span(
            "runner.turn", attributes=tracing.turn_attributes(self.name, self._sandbox)
        ) as turn_span:
            message_file = f".spleef-native/{self.name}-home/turn-{uuid.uuid4().hex}.txt"
            await self._upload_text(f"{WORKSPACE}/{message_file}", message)
            argv = [self.cli, *self._turn_args, message_file]
            result = await self._sandbox.exec(
                argv,
                cwd=WORKSPACE,
                user="agent",
                env=self.whitelist_env(),
                timeout_s=max(1, deadline_s),
            )
            duration = self._clock.now() - turn_start
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            stop, final_message = self._parse_result(stdout, stderr, result.exit_code)
            turn_span.set_attribute("runner.stop_reason", str(stop))
        return ShellTurn(
            stop_reason=stop,
            final_message=final_message,
            steps=1,
            usage=TokenUsage(),
            cost_usd=0.0,
            duration_s=duration,
            trace_ref=None,
        )

    async def stop(self) -> None:
        """Idempotent: kill the CLI process group started by ``start``."""
        if not self._started:
            return
        self._started = False
        sandbox = self._sandbox
        pgid = self._pgid
        self._pgid = None
        if sandbox is None or pgid is None:
            return
        # Kill the process group first (setsid launch); fall back to the plain
        # pid for sandboxes where the CLI could not become a group leader.
        command = (
            f"kill -TERM -{pgid} 2>/dev/null || kill -TERM {pgid} 2>/dev/null; "
            f"kill -KILL -{pgid} 2>/dev/null || kill -KILL {pgid} 2>/dev/null; true"
        )
        await sandbox.exec(command, cwd=WORKSPACE, user="agent", timeout_s=10)

    # -- helpers ----------------------------------------------------------------------

    def whitelist_env(self) -> dict[str, str]:
        """The ``env -i`` style whitelist: PATH, HOME plus CLI-specific variables."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": self._home}
        env.update(self.cli_env())
        return env

    def _parse_result(
        self, stdout: str, stderr: str, exit_code: int
    ) -> tuple[StopReason, str | None]:
        for line in reversed(stdout.splitlines()):
            if line.startswith(RESULT_MARKER):
                try:
                    payload = json.loads(line[len(RESULT_MARKER) :])
                except json.JSONDecodeError:
                    break
                stop_raw = payload.get("stop_reason", "completed")
                try:
                    stop = StopReason(str(stop_raw))
                except ValueError:
                    stop = StopReason.CRASH
                message = payload.get("message")
                return stop, str(message) if message is not None else None
        if exit_code == 0:
            text = stdout.strip()
            return StopReason.COMPLETED, text if text else None
        return StopReason.CRASH, stderr.strip() or stdout.strip() or None

    async def _upload_text(self, remote: str, content: str) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            raise RuntimeError("runner is not started")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(content)
            local = Path(handle.name)
        try:
            await sandbox.upload(local, remote)
        finally:
            await asyncio.to_thread(local.unlink, missing_ok=True)


class ClaudeCodeRunner(NativeShellRunner):
    """Claude Code (``claude``) speaking the ``anthropic_messages`` protocol."""

    name = "claude-code"
    required_protocol = "anthropic_messages"
    cli = "claude"

    def cli_env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_BASE_URL": self._gateway_url,
            "ANTHROPIC_AUTH_TOKEN": self._trial_key,
            "ANTHROPIC_MODEL": self._model_alias,
            # Disables auto-updates, telemetry and error reporting (official env vars).
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }

    def config_files(self) -> dict[str, str]:
        # Disables the built-in WebSearch tool (settings key ``disabledTools``).
        settings = {"disabledTools": ["WebSearch"]}
        return {f"{self._home}/.claude/settings.json": json.dumps(settings, indent=2)}


class CodexCliRunner(NativeShellRunner):
    """OpenAI Codex CLI (``codex``) through a gateway ``model_providers`` entry."""

    name = "codex-cli"
    required_protocol = "chat_completions"
    cli = "codex"

    def cli_env(self) -> dict[str, str]:
        return {
            "SPLEEF_KEY": self._trial_key,
            "CODEX_HOME": self._home,
        }

    def config_files(self) -> dict[str, str]:
        config = f"""model = {json.dumps(self._model_alias)}
model_provider = "spleef"
check_for_update_on_startup = false
web_search = false

[analytics]
enabled = false

[feedback]
enabled = false

[model_providers.spleef]
name = "Spleef Gateway"
base_url = {json.dumps(f"{self._gateway_url}/v1")}
env_key = "SPLEEF_KEY"
wire_api = "chat"
"""
        return {f"{self._home}/config.toml": config}


class OpenCodeRunner(NativeShellRunner):
    """OpenCode (``opencode``) with an openai-compatible provider entry."""

    name = "opencode"
    required_protocol = "chat_completions"
    cli = "opencode"

    def cli_env(self) -> dict[str, str]:
        return {
            "SPLEEF_KEY": self._trial_key,
            "HOME": self._home,
        }

    def config_files(self) -> dict[str, str]:
        config = {
            "model": f"spleef/{self._model_alias}",
            "share": "disabled",
            "update": "disable",
            "provider": {
                "spleef": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Spleef Gateway",
                    "options": {
                        "baseURL": f"{self._gateway_url}/v1",
                        "apiKey": "{env:SPLEEF_KEY}",
                    },
                    "models": {self._model_alias: {"name": self._model_alias}},
                }
            },
            "permission": {"websearch": "deny", "webfetch": "deny"},
        }
        return {f"{self._home}/.config/opencode/opencode.json": json.dumps(config, indent=2)}
