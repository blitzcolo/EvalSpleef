"""Shared helpers for the observe tests (imported by path; test dirs are not packages)."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from evalspleef.control.batch.executor import LeaseHandle, PlannedTrial, TrialLeases
from evalspleef.control.taskpack import TaskPack
from evalspleef.control.trial.types import ModelEndpoint, TrialContext, WorkspacePaths
from evalspleef.core.models import Budget, EscalationPolicy
from evalspleef.spi.caps import NetMode
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxRole, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider

REPO = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = TemplateRef("oci", "python:3.12-slim", "sha256:" + "a" * 64)
START = 1_788_000_000.0


def pack_dir(task_id: str) -> Path:
    return REPO / "tasks" / task_id


def planned_trial(pack: TaskPack, trial_id: str = "observe.test.e1.t01") -> PlannedTrial:
    return PlannedTrial(
        trial_id=trial_id,
        batch_id="2026-11-b1",
        cell_id=f"{pack.task_id}.observe",
        task_id=pack.task_id,
        model="demo-model",
        shell="scripted",
        epoch=1,
        index=1,
        requirements=pack.requirements,
        template=TEMPLATE,
        agent_routes=("mock",),
        verifier_routes=("mock",),
        endpoint_id="ep_demo_00000000",
    )


def make_spec(trial: PlannedTrial, role: SandboxRole, lease_id: str) -> SandboxSpec:
    requirements = trial.requirements
    return SandboxSpec(
        trial_id=trial.trial_id,
        lease_id=lease_id,
        role=role,
        os=requirements.os,
        arch=sorted(requirements.arch)[0],
        template=trial.template,
        resources=Resources(requirements.cpus, requirements.memory_mb, requirements.disk_mb),
        network=NetworkPolicy(mode=requirements.network if role == "agent" else NetMode.OFF),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        labels={"batch": trial.batch_id, "trial": trial.trial_id, "lease": lease_id},
        max_lifetime_s=1800,
    )


async def make_ctx(
    provider: MockSandboxProvider,
    pack: TaskPack,
    tmp_path: Path,
    *,
    stage_agent: Callable[[MockSandbox], Awaitable[None]] | None = None,
) -> tuple[TrialContext, MockSandbox]:
    """A trial context backed by a real mock sandbox pair (agent + verifier)."""
    trial = planned_trial(pack)
    created = await provider.create(make_spec(trial, "agent", "lease-agent-0"))
    assert isinstance(created, MockSandbox)
    agent_sandbox: MockSandbox = created
    if stage_agent is not None:
        await stage_agent(agent_sandbox)

    async def open_verifier() -> LeaseHandle:
        sandbox = await provider.create(make_spec(trial, "verifier", "lease-verifier-1"))
        return LeaseHandle("lease-verifier-1", "verifier", provider.instance, None, sandbox)

    agent = LeaseHandle("lease-agent-0", "agent", provider.instance, None, agent_sandbox)
    workspace_block = pack.metadata.get("workspace")
    mount = "/workspace"
    starter: Path | None = None
    entries: tuple[str, ...] = ()
    if isinstance(workspace_block, dict):
        mount = str(workspace_block.get("mount") or "/workspace")
        starter_name = workspace_block.get("starter")
        if isinstance(starter_name, str) and starter_name:
            starter = pack.file(starter_name)
        raw_entries = workspace_block.get("entry_files") or ()
        entries = tuple(str(item) for item in raw_entries)
    ctx = TrialContext(
        trial=trial,
        pack=pack,
        endpoint=ModelEndpoint("ep_demo_00000000", "ep_demo_00000000", "http://gw.test/v1"),
        budget=Budget(),
        escalation=EscalationPolicy(
            f1_no_progress_rounds=pack.budget.escalation.f1_no_progress_rounds,
            f2_no_progress_rounds=pack.budget.escalation.f2_no_progress_rounds,
        ),
        workspace=WorkspacePaths(remote_root=mount, starter_dir=starter, entry_files=entries),
        agent=agent,
        leases=TrialLeases(agent, open_verifier),
        canary_token="tok",
        instruction="do the task",
        scratch_dir=tmp_path / "scratch",
    )
    ctx.scratch_dir.mkdir(parents=True, exist_ok=True)
    return ctx, agent_sandbox


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fixture_workspace(pack: TaskPack, tmp_path: Path, name: str) -> Path:
    """Rebuild the candidate tree a fixture scenario came from (self-contained)."""
    fixture = load_fixture(name)
    workspace = tmp_path / f"ws-{name.replace('.', '-')}"
    workspace.mkdir(parents=True)
    files = fixture.get("files")
    assert isinstance(files, dict), f"fixture {name} embeds no files"
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return workspace
