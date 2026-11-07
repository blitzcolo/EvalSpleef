from __future__ import annotations

import stat
from pathlib import Path

import pytest
from evalspleef_harbor_bridge.runner import HarborRunner

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.runner import RunnerBudget, StopReason
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver


@pytest.fixture
def stub_harbor(tmp_path: Path) -> Path:
    """Create a stub harbor script on tmp_path."""
    stub = tmp_path / "harbor"
    content = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "args = sys.argv[1:]\n"
        "log_dir = os.environ.get('HARBOR_LOG_DIR')\n"
        "\n"
        "if '--fail' in args:\n"
        "    sys.stderr.write('harbor run failed\\n')\n"
        "    sys.exit(1)\n"
        "\n"
        "if log_dir:\n"
        "    log_file = Path(log_dir) / 'eval.json'\n"
        "    log_file.write_text(json.dumps({\n"
        "        'status': 'success',\n"
        "        'message': 'harbor execution completed',\n"
        "        'steps': 3,\n"
        "        'cost_usd': 0.05,\n"
        "        'usage': {\n"
        "            'input_tokens': 150,\n"
        "            'output_tokens': 40,\n"
        "            'cached_tokens': 10,\n"
        "            'reasoning_tokens': 0\n"
        "        },\n"
        "        'trace_ref': 'trace-123'\n"
        "    }))\n"
        "\n"
        "print('Harbor task completed successfully')\n"
        "sys.exit(0)\n"
    )
    stub.write_text(content, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


@pytest.mark.asyncio
async def test_runner_successful_turn(stub_harbor: Path, tmp_path: Path):
    provider = MockSandboxProvider(
        instance="test-mock",
        config={"root_dir": str(tmp_path / "mock_root")},
        secrets=DictSecretResolver({}),
    )

    spec = SandboxSpec(
        trial_id="t1",
        lease_id="l1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="alpine:latest", digest="sha256:123"),
        resources=Resources(cpus=2, memory_mb=2048, disk_mb=4096),
        network=NetworkPolicy(mode=NetMode.FULL),
    )
    sandbox = await provider.create(spec)

    runner = HarborRunner(
        shell="claude-code",
        task="sample-task",
        executable=str(stub_harbor),
    )

    budget = RunnerBudget(max_total_tokens=10000, max_cost_usd=1.0, max_wall_clock_s=60)
    await runner.start(
        sandbox=sandbox,
        gateway_url="http://localhost:8080",
        trial_key="trial-secret-key",
        model_alias="gpt-4o",
        instruction="Fix the bug",
        budget=budget,
    )

    turn = await runner.send("hello", deadline_s=10)
    assert turn.stop_reason == StopReason.COMPLETED
    assert turn.final_message == "harbor execution completed"
    assert turn.steps == 3
    assert turn.cost_usd == 0.05
    assert turn.usage.input_tokens == 150
    assert turn.usage.output_tokens == 40
    assert turn.trace_ref == "trace-123"

    await runner.stop()
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_runner_failure_turn(stub_harbor: Path, tmp_path: Path):
    provider = MockSandboxProvider(
        instance="test-mock",
        config={"root_dir": str(tmp_path / "mock_root")},
        secrets=DictSecretResolver({}),
    )

    spec = SandboxSpec(
        trial_id="t1",
        lease_id="l1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="alpine:latest", digest="sha256:123"),
        resources=Resources(cpus=2, memory_mb=2048, disk_mb=4096),
        network=NetworkPolicy(mode=NetMode.FULL),
    )
    sandbox = await provider.create(spec)

    runner = HarborRunner(
        shell="claude-code",
        task="sample-task",
        executable=str(stub_harbor),
        extra_args=("--fail",),
    )

    budget = RunnerBudget(max_total_tokens=10000, max_cost_usd=1.0, max_wall_clock_s=60)
    await runner.start(
        sandbox=sandbox,
        gateway_url="http://localhost:8080",
        trial_key="trial-secret-key",
        model_alias="gpt-4o",
        instruction="Fix the bug",
        budget=budget,
    )

    turn = await runner.send("hello", deadline_s=10)
    assert turn.stop_reason == StopReason.CRASH
    assert turn.final_message == "harbor run failed"

    await runner.stop()
    await sandbox.destroy()
