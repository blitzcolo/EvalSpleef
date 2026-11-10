"""Judge sandboxes opened as leases: role judge, network off, declared zones, torn down."""

from __future__ import annotations

from dataclasses import replace

import pytest

from evalspleef.control.batch.executor import PlannedTrial
from evalspleef.control.observe.agent_bridge import JudgeSandboxRequest
from evalspleef.control.observe.agent_lease import LeaseJudgeOpener
from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily, RuntimeRequirements
from evalspleef.spi.errors import PolicyNotEnforceable
from evalspleef.spi.sandbox import WritablePath
from evalspleef.testing.mock_sandbox import MockSandbox

from .conftest import BATCH, TEMPLATE

ZONES = (WritablePath("/artifact", 32), WritablePath("/work", 320))


def _trial(*routes: str) -> PlannedTrial:
    requirements = RuntimeRequirements(
        os=OSFamily.LINUX,
        arch=frozenset({Arch.X86_64}),
        min_isolation=Isolation.L1,
        network=NetMode.FULL,  # the agent may have network; the judge never does
        max_lifetime_s=1800,
    )
    return PlannedTrial(
        trial_id="backend-001.m.terminus.e1.t01",
        batch_id=BATCH,
        cell_id="backend-001.m.terminus",
        task_id="backend-001",
        model="m",
        shell="terminus",
        epoch=1,
        index=1,
        requirements=requirements,
        template=TEMPLATE,
        agent_routes=routes,
        verifier_routes=routes,
    )


async def test_a_judge_sandbox_is_a_network_off_lease_with_the_zones(env) -> None:
    caps = replace(env.provider.capabilities(), writable_paths=True, read_only_upload=True)
    opener = LeaseJudgeOpener(env.manager, lambda _name: caps)
    request = JudgeSandboxRequest(_trial("docker-a"), "R11", "vendor#1", ZONES)
    async with opener.open(request) as sandbox:
        assert isinstance(sandbox, MockSandbox)
        spec = sandbox.spec
        assert (spec.role, spec.network.mode, spec.writable_paths) == ("judge", NetMode.OFF, ZONES)
        assert spec.template == TEMPLATE
    assert env.manager.active == ()
    assert await env.event_types("backend-001.m.terminus.e1.t01") == [
        "sandbox.provisioned",
        "teardown.started",
        "teardown.completed",
    ]


async def test_no_capable_route_refuses_before_anything_is_created(env) -> None:
    caps = replace(env.provider.capabilities(), writable_paths=True, read_only_upload=False)
    opener = LeaseJudgeOpener(env.manager, lambda _name: caps)
    request = JudgeSandboxRequest(_trial("docker-a"), "R11", "vendor#1", ZONES)
    with pytest.raises(PolicyNotEnforceable, match="read_only_upload"):
        async with opener.open(request):
            pass
    assert await env.event_types() == []
