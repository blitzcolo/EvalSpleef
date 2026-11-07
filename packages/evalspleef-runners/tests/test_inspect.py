"""Tests for ``InspectRunner`` against a stub ``inspect`` executable on PATH.

Inspect AI is not installed in this environment, so the eval-log mapping is not
verified against real Inspect output.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from evalspleef.runners.inspect_runner import InspectRunner
from evalspleef.spi.runner import RunnerBudget, StopReason

GATEWAY_URL = "http://gateway.test:8080"

STUB = """#!/bin/sh
log="${INSPECT_LOG_DIR:?}"/run-1.json
cat > "$log" <<'EOF'
{"status": "success", "usage": {"input_tokens": 11, "output_tokens": 7, "cached_tokens": 2, "reasoning_tokens": 3}}
EOF
echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
echo "OPENAI_API_KEY=$OPENAI_API_KEY"
echo "ARGS=$*"
"""

MESSAGE_STUB = """#!/bin/sh
log="${INSPECT_LOG_DIR:?}"/run-1.json
cat > "$log" <<'EOF'
{"status": "success", "message": "log message wins"}
EOF
echo "stdout noise"
"""

FAILING_STUB = """#!/bin/sh
echo "boom" >&2
exit 2
"""

SLEEP_STUB = """#!/bin/sh
sleep 30
"""


@pytest.fixture
def inspect_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "inspect"
    script.write_text(STUB, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


async def start_inspect(runner: InspectRunner, sandbox: Any) -> None:
    await runner.start(
        sandbox,
        gateway_url=GATEWAY_URL,
        trial_key="trial-key-inspect",
        model_alias="ep_x",
        instruction="ignored",
        budget=RunnerBudget(max_total_tokens=1_000_000, max_cost_usd=100.0, max_wall_clock_s=3600),
    )


async def test_inspect_eval_maps_to_shell_turn(inspect_on_path: Path, sandbox: Any) -> None:
    runner = InspectRunner()
    await start_inspect(runner, sandbox)

    turn = await runner.send("run the eval", deadline_s=30)

    assert turn.stop_reason is StopReason.COMPLETED
    assert turn.final_message is not None
    assert "OPENAI_BASE_URL=http://gateway.test:8080" in turn.final_message
    assert "OPENAI_API_KEY=trial-key-inspect" in turn.final_message
    assert "openai/ep_x" in turn.final_message  # --model openai/<alias>
    assert turn.usage.input_tokens == 11
    assert turn.usage.output_tokens == 7
    assert turn.usage.cached_tokens == 2
    assert turn.usage.reasoning_tokens == 3
    await runner.stop()


async def test_log_message_preferred_over_stdout(inspect_on_path: Path, sandbox: Any) -> None:
    await asyncio.to_thread(inspect_on_path.write_text, MESSAGE_STUB, encoding="utf-8")
    runner = InspectRunner()
    await start_inspect(runner, sandbox)

    turn = await runner.send("run", deadline_s=30)

    assert turn.stop_reason is StopReason.COMPLETED
    assert turn.final_message == "log message wins"
    await runner.stop()


async def test_nonzero_exit_maps_to_crash(inspect_on_path: Path, sandbox: Any) -> None:
    await asyncio.to_thread(inspect_on_path.write_text, FAILING_STUB, encoding="utf-8")
    runner = InspectRunner()
    await start_inspect(runner, sandbox)

    turn = await runner.send("run", deadline_s=30)

    assert turn.stop_reason is StopReason.CRASH
    assert turn.final_message == "boom"
    await runner.stop()


async def test_deadline_maps_to_timeout(inspect_on_path: Path, sandbox: Any) -> None:
    await asyncio.to_thread(inspect_on_path.write_text, SLEEP_STUB, encoding="utf-8")
    runner = InspectRunner()
    await start_inspect(runner, sandbox)

    turn = await runner.send("run", deadline_s=1)

    assert turn.stop_reason is StopReason.TIMEOUT
    await runner.stop()


async def test_send_before_start_raises() -> None:
    runner = InspectRunner()
    with pytest.raises(RuntimeError):
        await runner.send("run", deadline_s=30)
