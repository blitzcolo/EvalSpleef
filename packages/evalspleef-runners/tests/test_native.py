"""Tests for the native in-sandbox runners against a stub CLI in a MockSandbox.

The stub emulates the ``--serve`` / ``--turn`` protocol the runner speaks and dumps
its environment so the whitelist can be asserted. The real CLIs are not installed:
these tests are not verified against the real Claude Code, Codex CLI or OpenCode.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from evalspleef.runners.native_shell import (
    ClaudeCodeRunner,
    CodexCliRunner,
    NativeShellRunner,
    OpenCodeRunner,
)
from evalspleef.spi.runner import RunnerBudget, StopReason
from evalspleef.spi.sandbox import Sandbox
from evalspleef.testing.mock_sandbox import MockSandbox

GATEWAY_URL = "http://gateway.test:8080"
MODEL_ALIAS = "ep_x"

# One stub serves all three CLIs; the runner passes only PATH/HOME plus the
# CLI-specific variables, so `env` inside the stub is exactly the whitelist
# (plus the mock sandbox base env, which the SPI always injects).
STUB = """#!/bin/sh
mode="$1"
case "$mode" in
  --serve)
    exec sleep 1000
    ;;
  --turn)
    msgfile="$2"
    message=$(cat "$msgfile")
    if echo "$message" | grep -q FAIL; then
      echo "boom" >&2
      exit 3
    fi
    echo "turn for: $message"
    # cwd is /workspace for every turn exec; write relative to it.
    env | sort > .spleef-env.dump
    if echo "$message" | grep -q CLARIFY; then
      echo 'SPLEEF_RESULT {"stop_reason": "need_clarification", "message": "clarify please"}'
    else
      echo 'SPLEEF_RESULT {"stop_reason": "completed", "message": "stub turn done"}'
    fi
    ;;
  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac
"""


@pytest.fixture
def stub_cli(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "stub-cli"
    script.parent.mkdir(parents=True)
    script.write_text(STUB, encoding="utf-8")
    script.chmod(0o755)
    return script


def make_budget() -> RunnerBudget:
    return RunnerBudget(max_total_tokens=1_000_000, max_cost_usd=100.0, max_wall_clock_s=3600)


async def start_native(runner: NativeShellRunner, sandbox: Sandbox, trial_key: str) -> None:
    await runner.start(
        sandbox,
        gateway_url=GATEWAY_URL,
        trial_key=trial_key,
        model_alias=MODEL_ALIAS,
        instruction="ignored by the stub",
        budget=make_budget(),
    )


async def sandbox_file(sandbox: Sandbox, path: str) -> str:
    """Read a sandbox file; the mock sandbox remaps cwd, not argv paths."""
    prefix = "/workspace/"
    if path.startswith(prefix):
        result = await sandbox.exec(["cat", path[len(prefix) :]], cwd="/workspace")
    else:
        result = await sandbox.exec(["cat", path])
    assert result.exit_code == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("utf-8", errors="replace")


def parse_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


async def read_env_dump(sandbox: Sandbox, home: str) -> dict[str, str]:
    del home  # the stub dumps the env relative to /workspace
    return parse_env(await sandbox_file(sandbox, "/workspace/.spleef-env.dump"))


async def test_claude_code_env_whitelist_and_config(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = ClaudeCodeRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-claude")

    turn = await runner.send("hello", deadline_s=30)
    assert turn.stop_reason is StopReason.COMPLETED
    assert turn.final_message == "stub turn done"

    home = "/workspace/.spleef-native/claude-code-home"
    env = await read_env_dump(sandbox, home)
    assert env["ANTHROPIC_BASE_URL"] == GATEWAY_URL
    assert env["ANTHROPIC_AUTH_TOKEN"] == "trial-key-claude"
    assert env["ANTHROPIC_MODEL"] == MODEL_ALIAS
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["HOME"] == home
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    # No other CLI's variables or extra secrets leak into the environment.
    assert "SPLEEF_KEY" not in env
    assert "CODEX_HOME" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env

    settings = json.loads(await sandbox_file(sandbox, f"{home}/.claude/settings.json"))
    assert settings == {"disabledTools": ["WebSearch"]}
    await runner.stop()


async def test_codex_config_and_env(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = CodexCliRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-codex")

    turn = await runner.send("hello", deadline_s=30)
    assert turn.stop_reason is StopReason.COMPLETED

    home = "/workspace/.spleef-native/codex-cli-home"
    config = tomllib.loads(await sandbox_file(sandbox, f"{home}/config.toml"))
    assert config["model"] == MODEL_ALIAS
    assert config["model_provider"] == "spleef"
    assert config["check_for_update_on_startup"] is False
    assert config["web_search"] is False
    assert config["analytics"] == {"enabled": False}
    assert config["feedback"] == {"enabled": False}
    provider = config["model_providers"]["spleef"]
    assert provider["base_url"] == f"{GATEWAY_URL}/v1"
    assert provider["env_key"] == "SPLEEF_KEY"
    assert provider["wire_api"] == "chat"

    env = await read_env_dump(sandbox, home)
    assert env["SPLEEF_KEY"] == "trial-key-codex"
    assert env["CODEX_HOME"] == home
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    await runner.stop()


async def test_opencode_config_and_env(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = OpenCodeRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-opencode")

    turn = await runner.send("hello", deadline_s=30)
    assert turn.stop_reason is StopReason.COMPLETED

    home = "/workspace/.spleef-native/opencode-home"
    config = json.loads(await sandbox_file(sandbox, f"{home}/.config/opencode/opencode.json"))
    assert config["model"] == f"spleef/{MODEL_ALIAS}"
    assert config["share"] == "disabled"
    assert config["update"] == "disable"
    provider = config["provider"]["spleef"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == f"{GATEWAY_URL}/v1"
    assert provider["options"]["apiKey"] == "{env:SPLEEF_KEY}"
    assert config["permission"] == {"websearch": "deny", "webfetch": "deny"}

    env = await read_env_dump(sandbox, home)
    assert env["SPLEEF_KEY"] == "trial-key-opencode"
    assert env["HOME"] == home
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    await runner.stop()


async def test_stop_kills_cli_process_group(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = ClaudeCodeRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-claude")
    pgid = runner._pgid  # white-box check of the launched process
    assert pgid is not None
    alive = await sandbox.exec(["kill", "-0", str(pgid)])
    assert alive.exit_code == 0

    await runner.stop()
    gone = await sandbox.exec(["kill", "-0", str(pgid)])
    assert gone.exit_code != 0
    # stop() is idempotent.
    await runner.stop()


async def test_nonzero_exit_maps_to_crash(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = OpenCodeRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-opencode")

    turn = await runner.send("this will FAIL", deadline_s=30)

    assert turn.stop_reason is StopReason.CRASH
    assert turn.final_message == "boom"
    await runner.stop()


async def test_result_marker_maps_stop_reasons(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = CodexCliRunner(cli=str(stub_cli))
    await start_native(runner, sandbox, trial_key="trial-key-codex")

    turn = await runner.send("please CLARIFY this", deadline_s=30)

    assert turn.stop_reason is StopReason.NEED_CLARIFICATION
    assert turn.final_message == "clarify please"
    await runner.stop()


async def test_send_before_start_raises(sandbox: MockSandbox, stub_cli: Path) -> None:
    runner = ClaudeCodeRunner(cli=str(stub_cli))
    with pytest.raises(RuntimeError):
        await runner.send("hello", deadline_s=30)
