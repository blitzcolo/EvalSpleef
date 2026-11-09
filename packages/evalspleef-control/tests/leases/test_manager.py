"""Lease manager: labels, rows, events, guaranteed teardown, invariant I10 and orphan reaping."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from evalspleef.control.leases import LeaseInvariantError
from evalspleef.core.events import EventType
from evalspleef.spi.caps import NetMode, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.sandbox import NetworkPolicy, Sandbox, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider

BATCH = "2026-11-b1"
TRIAL = "web-001.alpha.terminus.e1.t01"


def mock_sandbox(sandbox: Sandbox) -> MockSandbox:
    assert isinstance(sandbox, MockSandbox)
    return sandbox


async def test_provision_labels_ttl_row_and_events(env, request_for) -> None:
    leased = await env.manager.provision(request_for())

    spec = mock_sandbox(leased.sandbox).spec
    assert spec.labels == {
        "batch": BATCH,
        "trial": TRIAL,
        "lease": leased.lease_id,
        "provider": "docker-a",
    }
    assert spec.max_lifetime_s == 1800
    assert leased.ttl_s == 1800
    row = await env.leases.get(leased.lease_id)
    assert row is not None
    assert (row.batch_id, row.trial_id, row.role) == (BATCH, TRIAL, "agent")
    assert (row.provider_instance, row.host_id) == ("docker-a", "host-1")
    assert row.template_digest.startswith("sha256:")
    assert row.destroyed_at is None
    assert await env.event_types(TRIAL) == ["trial.leased", "sandbox.provisioned"]
    provisioned = (await env.events_of(EventType.SANDBOX_PROVISIONED))[0]
    assert provisioned.payload["lease_id"] == leased.lease_id
    assert provisioned.payload["sandbox_id"] == leased.sandbox.id
    await env.manager.teardown(leased)


async def test_every_new_sandbox_is_measured_and_the_guest_recorded(env, request_for) -> None:
    agent = await env.manager.provision(request_for())
    verifier = await env.manager.provision(request_for(role="verifier"))

    provisioned = await env.events_of(EventType.SANDBOX_PROVISIONED)
    assert [event.payload["role"] for event in provisioned] == ["agent", "verifier"]
    for leased, event in zip((agent, verifier), provisioned, strict=True):
        # The mock runs commands on this machine, so the guest is this machine's kernel.
        assert event.payload["guest"]["status"] == "measured"
        assert event.payload["guest"]["guest"]["kernel"] == os.uname().release
        assert leased.guest == event.payload["guest"]
    await env.manager.teardown(verifier)
    await env.manager.teardown(agent)


class _UnmeasurableProvider(MockSandboxProvider):
    """Creates sandboxes that vanish before anything can run in them."""

    async def create(self, spec: SandboxSpec) -> Sandbox:
        sandbox = await super().create(spec)
        mock_sandbox(sandbox).mark_lost()
        return sandbox


async def test_an_unmeasurable_guest_is_recorded_and_the_lease_still_succeeds(
    env, request_for
) -> None:
    provider = _UnmeasurableProvider("docker-a", {}, env.provider.secrets, clock=env.clock)
    manager = env.make_manager({"docker-a": provider})

    leased = await manager.provision(request_for())

    (provisioned,) = await env.events_of(EventType.SANDBOX_PROVISIONED)
    assert provisioned.payload["guest"]["status"] == "unmeasured"
    assert "SandboxLost" in provisioned.payload["guest"]["reason"]
    assert leased.guest == provisioned.payload["guest"]
    assert await env.event_types(TRIAL) == ["trial.leased", "sandbox.provisioned"]
    await manager.teardown(leased)


async def test_provision_boots_the_carrier_template_and_records_it(env, request_for) -> None:
    calls: list[tuple[TemplateRef, str]] = []

    def carrier(template: TemplateRef, instance: str) -> TemplateRef:
        calls.append((template, instance))
        return TemplateRef(kind="novita_template", ref="tpl-123", digest=template.digest)

    request = request_for()
    leased = await env.make_manager(templates=carrier).provision(request)

    assert calls == [(request.template, "docker-a")]
    booted = mock_sandbox(leased.sandbox).spec.template
    assert (booted.kind, booted.ref, booted.digest) == (
        "novita_template",
        "tpl-123",
        request.template.digest,
    )
    row = await env.leases.get(leased.lease_id)
    assert row is not None
    assert (row.template_kind, row.template_ref) == ("novita_template", "tpl-123")
    await env.manager.teardown(leased)


async def test_lease_destroys_the_sandbox_when_the_body_raises(env, request_for) -> None:
    created: list[Sandbox] = []

    with pytest.raises(RuntimeError, match="boom"):
        async with env.manager.lease(request_for()) as leased:
            created.append(leased.sandbox)
            raise RuntimeError("boom")

    assert mock_sandbox(created[0]).destroyed
    row = await env.leases.get(leased.lease_id)
    assert row is not None
    assert row.destroyed_at is not None
    assert not row.reaped
    assert await env.event_types(TRIAL) == [
        "trial.leased",
        "sandbox.provisioned",
        "teardown.started",
        "teardown.completed",
    ]
    completed = (await env.events_of(EventType.TEARDOWN_COMPLETED))[0]
    assert completed.payload["ok"] is True
    assert env.manager.active == ()


async def test_teardown_is_idempotent(env, request_for) -> None:
    leased = await env.manager.provision(request_for())

    await env.manager.teardown(leased)
    await env.manager.teardown(leased)

    assert (await env.event_types(TRIAL)).count("teardown.completed") == 1


async def test_failed_destroy_is_recorded_and_leaves_the_lease_for_the_reaper(
    env, request_for
) -> None:
    leased = await env.manager.provision(request_for())
    sandbox = mock_sandbox(leased.sandbox)
    real_destroy = sandbox.destroy

    async def failing_destroy() -> None:
        raise OSError("device busy")

    sandbox.destroy = failing_destroy  # type: ignore[method-assign]
    await env.manager.teardown(leased)

    completed = (await env.events_of(EventType.TEARDOWN_COMPLETED))[0]
    assert completed.payload["ok"] is False
    assert "device busy" in completed.payload["error"]
    assert not leased.destroyed
    assert leased.teardown_error is not None
    row = await env.leases.get(leased.lease_id)
    assert row is not None
    assert row.destroyed_at is None
    assert env.manager.active == (leased,)

    sandbox.destroy = real_destroy  # type: ignore[method-assign]
    await env.manager.teardown(leased)  # a later attempt succeeds
    assert leased.destroyed
    assert env.manager.active == ()


async def test_verifier_is_a_separate_lease_on_a_separate_sandbox_with_network_off(
    env, request_for
) -> None:
    full_network = request_for(network=NetworkPolicy(mode=NetMode.FULL))
    async with (
        env.manager.lease(full_network) as agent,
        env.manager.lease(request_for(role="verifier")) as verifier,
    ):
        assert verifier.lease_id != agent.lease_id
        assert verifier.sandbox.id != agent.sandbox.id
        assert mock_sandbox(verifier.sandbox).spec.network.mode == NetMode.OFF
        assert mock_sandbox(agent.sandbox).spec.network.mode == NetMode.FULL
        rows = await env.leases.list_by_trial(TRIAL)
        assert sorted(r.role for r in rows) == ["agent", "verifier"]
        assert {r.lease_id for r in rows} == {agent.lease_id, verifier.lease_id}

    assert (await env.event_types(TRIAL)).count("trial.leased") == 1  # agent only
    assert (await env.event_types(TRIAL)).count("sandbox.provisioned") == 2


async def test_verifier_network_is_off_even_when_the_task_allows_network(env, request_for) -> None:
    networked = replace(request_for().requirements, network=NetMode.FULL, allow_hosts=("pypi.org",))

    async with env.manager.lease(request_for(role="verifier", requirements=networked)) as verifier:
        policy = mock_sandbox(verifier.sandbox).spec.network

    assert policy.mode == NetMode.OFF
    assert policy.allow_hosts == ()


async def test_verifier_cannot_ask_for_network(env, request_for) -> None:
    request = request_for(role="verifier", network=NetworkPolicy(mode=NetMode.FULL))

    with pytest.raises(LeaseInvariantError):
        await env.manager.provision(request)

    assert env.provider.created_sandboxes() == []


class SharingProvider(MockSandboxProvider):
    """A broken provider that hands the agent's live sandbox to the verifier (an I10 breach)."""

    shared: Sandbox | None = None

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if spec.role == "verifier" and self.shared is not None:
            return self.shared
        sandbox = await super().create(spec)
        self.shared = sandbox
        return sandbox


async def test_verifier_that_would_share_the_agent_sandbox_is_refused(env, request_for) -> None:
    provider = SharingProvider("docker-a", {}, env.provider.secrets, clock=env.clock)
    manager = env.make_manager({"docker-a": provider})
    agent = await manager.provision(request_for())

    with pytest.raises(LeaseInvariantError, match="I10"):
        await manager.provision(request_for(role="verifier"))

    assert not mock_sandbox(agent.sandbox).destroyed  # the agent's sandbox is untouched
    assert [r.role for r in await env.leases.list_by_trial(TRIAL)] == ["agent"]
    await manager.teardown(agent)


async def test_capability_rejection_is_recorded_and_nothing_is_created(env, request_for) -> None:
    windows = replace(request_for().requirements, os=OSFamily.WINDOWS)

    with pytest.raises(CapabilityNotSupported):
        await env.manager.provision(request_for(requirements=windows))

    assert env.provider.created_sandboxes() == []
    assert await env.leases.list_by_trial(TRIAL) == []
    rejected = (await env.events_of(EventType.PROVIDER_CAPABILITY_REJECT))[0]
    assert rejected.payload["provider"] == "docker-a"
    assert rejected.payload["error"] == "CapabilityNotSupported"


async def test_sandbox_is_destroyed_when_the_lease_row_cannot_be_recorded(env, request_for) -> None:
    manager = env.make_manager(new_id=lambda: "same")
    first = await manager.provision(request_for("web-001.alpha.terminus.e1.t01"))

    with pytest.raises(ValueError, match="already exists"):
        await manager.provision(request_for("web-002.alpha.terminus.e1.t01"))

    sandboxes = env.provider.created_sandboxes()
    assert [s.destroyed for s in sandboxes] == [False, True]
    await manager.teardown(first)


async def test_reaper_destroys_only_orphans_and_marks_their_rows(env, request_for) -> None:
    orphan = await env.manager.provision(request_for())  # the control plane "forgets" it
    env.clock.advance(1800 + 600 + 1)
    fresh = await env.manager.provision(request_for("web-002.alpha.terminus.e1.t01"))

    reaped = await env.manager.reap_once(BATCH)

    assert reaped == 1
    assert mock_sandbox(orphan.sandbox).destroyed
    assert not mock_sandbox(fresh.sandbox).destroyed
    row = await env.leases.get(orphan.lease_id)
    assert row is not None
    assert row.reaped
    assert row.destroyed_at is not None
    event = (await env.events_of(EventType.SANDBOX_ORPHAN_REAPED))[0]
    assert event.trial_id == TRIAL
    assert event.payload["lease_id"] == orphan.lease_id
    assert event.payload["provider"] == "docker-a"
    assert [lease.lease_id for lease in env.manager.active] == [fresh.lease_id]
    assert await env.manager.reap_once(BATCH) == 0
    await env.manager.teardown(fresh)


async def test_orphans_are_sandboxes_no_live_lease_accounts_for(env, request_for) -> None:
    lost_track = await env.manager.provision(request_for())
    # A control plane that restarted knows no live leases and mints fresh lease ids.
    restarted = env.make_manager(new_id=lambda: "after-restart")
    tracked = await restarted.provision(request_for("web-002.alpha.terminus.e1.t01"))

    orphans = await restarted.find_orphans(BATCH)

    assert [o.lease_id for o in orphans] == [lost_track.lease_id]
    assert orphans[0].trial_id == TRIAL
    assert orphans[0].instance == "docker-a"
    # Each manager sees the other's lease, and only the other's, as unaccounted for.
    assert [o.lease_id for o in await env.manager.find_orphans(BATCH)] == [tracked.lease_id]


async def test_reaper_loop_reaps_every_interval_until_stopped(env, request_for) -> None:
    class Ticks:
        def __init__(self) -> None:
            self.requested: list[float] = []
            self.queue: asyncio.Queue[None] = asyncio.Queue()

        async def sleep(self, seconds: float) -> None:
            self.requested.append(seconds)
            await self.queue.get()

    ticks = Ticks()
    manager = env.make_manager(sleep=ticks.sleep)
    orphan = await env.manager.provision(request_for())
    env.clock.advance(1800 + 600 + 1)
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run_reaper(BATCH, stop))

    ticks.queue.put_nowait(None)
    async with asyncio.timeout(5):
        async for event in env.events.subscribe(BATCH, wait_s=5.0):
            if event.type == EventType.SANDBOX_ORPHAN_REAPED:
                assert event.payload["lease_id"] == orphan.lease_id
                break
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert ticks.requested[0] == 600.0
    assert mock_sandbox(orphan.sandbox).destroyed
