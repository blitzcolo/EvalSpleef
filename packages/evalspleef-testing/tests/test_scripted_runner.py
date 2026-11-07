from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.runner import AgentRunner, RunnerBudget, ShellTurn, StopReason, TokenUsage
from evalspleef.spi.sandbox import NetworkPolicy, Resources, Sandbox, SandboxSpec, TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner
from evalspleef.testing.secrets import DictSecretResolver

DIGEST = "sha256:" + "0" * 64
INSTRUCTION = "make the failing test pass"
TRIAL_KEY = "sk-spleef-test"
ROUTE = "ep_qwen_test"
BUDGET = RunnerBudget(max_total_tokens=100_000, max_cost_usd=1.0, max_wall_clock_s=600)


def make_spec() -> SandboxSpec:
    return SandboxSpec(
        trial_id="t1",
        lease_id="l1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="mock:latest", digest=DIGEST),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=NetMode.OFF),
        labels={"batch": "b1", "trial": "t1", "lease": "l1", "provider": "mock-sandbox"},
        max_lifetime_s=3600,
    )


@pytest.fixture
async def sandbox() -> AsyncIterator[Sandbox]:
    provider = MockSandboxProvider(
        "scripted-1", {}, DictSecretResolver(), clock=FakeClock(start=1000.0)
    )
    created = await provider.create(make_spec())
    try:
        yield created
    finally:
        await created.destroy()


async def start(runner: ScriptedRunner, sandbox: Sandbox) -> None:
    await runner.start(
        sandbox,
        gateway_url="http://gateway.test/v1",
        trial_key=TRIAL_KEY,
        model_alias=ROUTE,
        instruction=INSTRUCTION,
        budget=BUDGET,
    )


async def read(sandbox: Sandbox, path: str) -> str:
    result = await sandbox.exec(["cat", path])
    assert result.exit_code == 0, result.stderr
    return result.stdout.decode()


async def test_empty_script_models_the_empty_agent(sandbox: Sandbox) -> None:
    runner = ScriptedRunner()
    assert isinstance(runner, AgentRunner)

    await start(runner, sandbox)
    turn = await runner.send(INSTRUCTION, deadline_s=60)

    assert turn == ShellTurn(
        stop_reason=StopReason.COMPLETED,
        final_message=None,
        steps=0,
        usage=TokenUsage(),
        cost_usd=0.0,
        duration_s=0.0,
    )
    listing = await sandbox.exec(["ls", "-A"])
    assert listing.stdout == b""
    assert runner.exec_results == []
    assert runner.received_messages == [INSTRUCTION]


async def test_a_round_runs_shell_commands_and_file_writes(sandbox: Sandbox) -> None:
    runner = ScriptedRunner(
        [
            ScriptedRound(
                commands=("printf 'hello' > greeting.txt",),
                files={"notes/answer.txt": "42\n"},
                final_message="done",
            )
        ]
    )
    await start(runner, sandbox)

    turn = await runner.send(INSTRUCTION, deadline_s=60)

    assert turn.stop_reason == StopReason.COMPLETED
    assert turn.final_message == "done"
    assert turn.steps == 2
    assert await read(sandbox, "greeting.txt") == "hello"
    assert await read(sandbox, "notes/answer.txt") == "42\n"
    assert len(runner.exec_results) == 1
    assert runner.exec_results[0].exit_code == 0


async def test_per_round_scripts_model_the_feedback_loop(sandbox: Sandbox) -> None:
    runner = ScriptedRunner(
        [
            ScriptedRound(
                commands=("printf 'draft' > answer.txt",),
                stop_reason=StopReason.NEED_CLARIFICATION,
                final_message="Should I also update the docs?",
            ),
            ScriptedRound(
                commands=("printf 'final' > answer.txt",),
                stop_reason=StopReason.COMPLETED,
                final_message="final answer written",
            ),
        ]
    )
    await start(runner, sandbox)

    first = await runner.send(INSTRUCTION, deadline_s=60)
    assert first.stop_reason == StopReason.NEED_CLARIFICATION
    assert await read(sandbox, "answer.txt") == "draft"

    second = await runner.send("No, leave the docs alone.", deadline_s=60)
    assert second.stop_reason == StopReason.COMPLETED
    assert await read(sandbox, "answer.txt") == "final"
    assert runner.received_messages == [INSTRUCTION, "No, leave the docs alone."]


async def test_round_reports_configured_usage_cost_and_stop_reason(sandbox: Sandbox) -> None:
    usage = TokenUsage(input_tokens=100, output_tokens=40, cached_tokens=10, reasoning_tokens=5)
    runner = ScriptedRunner(
        [
            ScriptedRound(
                stop_reason=StopReason.TOKEN_CAP,
                final_message="budget exhausted",
                usage=usage,
                cost_usd=0.25,
                duration_s=1.5,
            )
        ]
    )
    await start(runner, sandbox)

    turn = await runner.send(INSTRUCTION, deadline_s=60)

    assert turn.stop_reason == StopReason.TOKEN_CAP
    assert turn.final_message == "budget exhausted"
    assert turn.steps == 0
    assert turn.usage == usage
    assert turn.cost_usd == 0.25
    assert turn.duration_s == 1.5


async def test_an_exhausted_script_does_not_repeat_side_effects(sandbox: Sandbox) -> None:
    runner = ScriptedRunner([ScriptedRound(commands=("printf 'once' >> count.txt",))])
    await start(runner, sandbox)

    first = await runner.send(INSTRUCTION, deadline_s=60)
    second = await runner.send("please continue", deadline_s=60)

    assert first.steps == 1
    assert second.steps == 0
    assert second.stop_reason == StopReason.COMPLETED
    assert await read(sandbox, "count.txt") == "once"


async def test_start_records_the_trial_context(sandbox: Sandbox) -> None:
    runner = ScriptedRunner()
    await start(runner, sandbox)

    assert runner.instruction == INSTRUCTION
    assert runner.trial_key == TRIAL_KEY
    assert runner.model_alias == ROUTE
    assert runner.gateway_url == "http://gateway.test/v1"
    assert runner.budget == BUDGET


async def test_send_requires_start_and_stop_is_idempotent(sandbox: Sandbox) -> None:
    runner = ScriptedRunner()
    with pytest.raises(RuntimeError, match="start"):
        await runner.send(INSTRUCTION, deadline_s=60)

    await start(runner, sandbox)
    await runner.stop()
    await runner.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        await runner.send(INSTRUCTION, deadline_s=60)
