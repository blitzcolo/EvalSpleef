"""Carrier caps count sandboxes: a batch waits for capacity instead of failing trials.

Regression cover for real batch 2026-11-b6 (2026-11-10): a hostd daemon capped its Docker
provider at 4 sandboxes while the control plane admitted 4 trials, each needing an agent
and a verifier sandbox. Every verifier create was refused with ``QuotaExceeded``, recorded
as ``failed_infra`` and retried, invalidating 20 trials after their agents had spent model
tokens. The fix reserves the verifier slot with the agent slot at admission and treats a
carrier's ``QuotaExceeded`` as capacity pressure to wait out, not an infrastructure failure.
"""

from __future__ import annotations

import asyncio

from evalspleef.control.scheduling import CapacitySpec
from evalspleef.core.batch import BatchState
from evalspleef.core.events import EventEnvelope, EventType

BATCH = "2026-11-b1"


async def peak_live_sandboxes(events: list[EventEnvelope]) -> int:
    """Highest number of concurrently live sandboxes in the provision/teardown timeline."""
    live = 0
    peak = 0
    for event in events:  # the store reads in append order
        if event.type is EventType.SANDBOX_PROVISIONED:
            live += 1
            peak = max(peak, live)
        elif event.type is EventType.TEARDOWN_COMPLETED and event.payload.get("ok"):
            live -= 1
    return peak


async def test_a_capped_carrier_runs_every_trial_without_quota_incidents(build) -> None:
    """Carrier cap == instance limit == 2, four trials: all complete, nothing exceeds 2."""
    h = await build(
        n_tasks=4,
        capacity=[CapacitySpec("docker-a", configured_limit=2)],
        provider_quota=2,  # the carrier itself refuses a third sandbox
    )
    async with asyncio.timeout(30):  # bounds a deadlock: no fixed waits anywhere below
        result = await h.freeze_and_run()

    assert result.state is BatchState.GRADED
    rows = await h.trials.list_by_batch(BATCH)
    assert len(rows) == 4
    assert {row.state for row in rows} == {"scored"}
    # No invalidations and no incidents at all: no trial hit QuotaExceeded.
    assert await h.events_of(EventType.TRIAL_INVALIDATED) == []
    assert await h.events_of(EventType.INCIDENT_OPENED) == []
    # The trial admits itself two slots (agent + verifier), one trial at a time.
    assert h.executor.peak == 1
    assert [len(await h.lease_rows.list_by_trial(row.trial_id)) for row in rows] == [2] * 4
    assert all(sandbox.destroyed for sandbox in h.providers["docker-a"].created_sandboxes())
    assert len(h.providers["docker-a"].created_sandboxes()) == 8
    # The event log is the timeline: at no moment were more sandboxes live than the cap.
    assert await peak_live_sandboxes(await h.events.read(BATCH)) == 2


async def test_quota_pressure_that_outlives_the_wait_budget_fails_cleanly(build) -> None:
    """A carrier stuck at its cap fails the lease with a clear reason after the budget."""
    h = await build(
        capacity=[CapacitySpec("docker-a", configured_limit=4)],  # the scheduler admits...
        provider_quota=1,  # ...but the carrier hosts only one sandbox
        lease_kwargs={"quota_wait_budget_s": 0.05, "quota_wait_poll_s": 0.01},
    )
    async with asyncio.timeout(30):  # bounded: exhaustion must fail, never hang
        await h.freeze_and_run()

    rows = await h.trials.list_by_batch(BATCH)
    assert {row.state for row in rows} == {"invalid"}
    incidents = await h.events_of(EventType.INCIDENT_OPENED)
    quota_incidents = [e for e in incidents if e.payload.get("kind") == "sandbox_quota_exhausted"]
    assert len(quota_incidents) == len(rows)
    invalidated = await h.events_of(EventType.TRIAL_INVALIDATED)
    reasons = " ".join(str(e.payload.get("reason")) for e in invalidated)
    assert "quota wait budget is exhausted" in reasons
    # Every attempt's agent sandbox was torn down when its verifier could not be leased.
    assert all(sandbox.destroyed for sandbox in h.providers["docker-a"].created_sandboxes())
    assert h.scheduler.capacity("docker-a").in_use == 0
