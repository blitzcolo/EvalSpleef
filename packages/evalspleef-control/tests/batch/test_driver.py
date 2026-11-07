"""Batch driver: concurrency under the capacity ceiling, retries, lost hosts and teardown."""

from __future__ import annotations

import asyncio

import pytest

from evalspleef.control.scheduling import CapacitySpec, HostSpec, next_trial_id
from evalspleef.core.batch import BatchState, IllegalBatchTransition
from evalspleef.core.events import EventType
from evalspleef.core.models import CellStatus, Validity
from evalspleef.spi.errors import CapabilityNotSupported, SandboxCreateError, SandboxLost

BATCH = "2026-11-b1"
TRIAL = "web-001.alpha.terminus.e1.t01"


def attempt_ids(count: int) -> list[str]:
    return [f"web-001.alpha.terminus.e1.t{n:02d}" for n in range(1, count + 1)]


async def test_thirty_trials_run_concurrently_within_the_capacity_ceiling(build) -> None:
    # The ceiling counts trials. A trial briefly holds its agent and its verifier sandbox, so the
    # provider quota behind the ceiling is twice as high.
    h = await build(
        n_models=3,
        n_tasks=10,
        capacity=[
            CapacitySpec(
                "docker-a", declared_max_concurrency=8, preflight_quota=6, configured_limit=4
            )
        ],
        provider_quota=8,
    )
    assert len(h.plan.trials) == 30
    h.executor.gate = asyncio.Event()  # hold the first trials inside the executor
    h.driver.freeze(lint_passed=True, budget_approved=True)
    run = asyncio.create_task(h.driver.run())
    async with asyncio.timeout(10):
        for _ in range(4):
            await h.executor.arrived.get()
    # Four trials are in flight and the other 26 wait: the ceiling holds at its limit.
    assert h.executor.running == 4
    assert h.scheduler.capacity("docker-a").in_use == 4
    assert h.scheduler.pending == 26
    h.executor.gate.set()

    result = await asyncio.wait_for(run, timeout=30)

    assert h.executor.peak == 4
    rows = await h.trials.list_by_batch(BATCH)
    assert len(rows) == 30
    assert {row.state for row in rows} == {"scored"}
    assert result.state is BatchState.GRADED
    assert h.scheduler.capacity("docker-a").in_use == 0
    sandboxes = h.providers["docker-a"].created_sandboxes()
    assert len(sandboxes) == 60  # one agent and one verifier sandbox per trial
    assert all(sandbox.destroyed for sandbox in sandboxes)
    for row in rows:
        leases = await h.lease_rows.list_by_trial(row.trial_id)
        assert sorted(lease.role for lease in leases) == ["agent", "verifier"]
        assert all(lease.destroyed_at is not None for lease in leases)
    assert all(agent != verifier for agent, verifier in h.executor.leases_seen.values())
    for event_type, expected in [
        (EventType.TRIAL_CREATED, 30),
        (EventType.TRIAL_LEASED, 30),
        (EventType.TRIAL_SCORED, 30),
        (EventType.TEARDOWN_COMPLETED, 60),
    ]:
        assert len(await h.events_of(event_type)) == expected


async def test_results_reach_the_report_the_grade_table_and_the_freeze_hash(build) -> None:
    h = await build(n_models=2, n_tasks=2, epochs=3)  # core needs two counted epochs per cell

    result = await h.freeze_and_run()

    cells = result.report.build_cells()
    assert len(cells) == 4
    assert {cell.status for cell in cells} == {CellStatus.SCORED}
    assert {str(cell.tier) for cell in cells} == {"A"}
    assert {cell.trial_count for cell in cells} == {3}
    grades = await h.grades.list_by_trial(TRIAL)
    assert [(g.version, g.tier, g.status) for g in grades] == [(1, "A", "final")]
    assert result.version_matrix_hash == h.freeze.version_matrix_hash
    rows = await h.trials.list_by_batch(BATCH)
    assert {row.extensions["freeze_hash"] for row in rows} == {result.version_matrix_hash}


async def test_skipped_cells_appear_in_the_report_with_their_reason(build, task_factory) -> None:
    h = await build(tasks=[task_factory("web-001"), task_factory("web-002", gpu=True)], epochs=3)

    result = await h.freeze_and_run()

    cells = {cell.cell_id: cell for cell in result.report.build_cells()}
    assert cells["web-001.alpha.terminus"].status is CellStatus.SCORED
    assert cells["web-002.alpha.terminus"].status is CellStatus.SKIP
    assert cells["web-002.alpha.terminus"].skip_reason == "capability:gpu"
    assert sorted(h.executor.calls) == [f"web-001.alpha.terminus.e{n}.t01" for n in (1, 2, 3)]


async def test_running_before_freeze_is_an_illegal_transition(build) -> None:
    h = await build()

    with pytest.raises(IllegalBatchTransition):
        await h.driver.run()

    assert h.executor.calls == []


async def test_infra_failure_is_retried_as_a_new_record_with_the_next_index(build) -> None:
    h = await build()
    ids = attempt_ids(3)
    h.executor.raises[ids[0]] = SandboxLost("host went away")
    h.executor.raises[ids[1]] = SandboxLost("host went away again")

    result = await h.freeze_and_run()

    assert h.executor.calls == ids
    rows = {row.trial_id: row for row in await h.trials.list_by_batch(BATCH)}
    assert [(rows[i].state, rows[i].validity) for i in ids] == [
        ("invalid", "invalid"),
        ("invalid", "invalid"),
        ("scored", "valid"),
    ]
    assert [rows[i].extensions["retry_of"] for i in ids] == [None, ids[0], ids[1]]
    assert [rows[i].extensions["attempt"] for i in ids] == [0, 1, 2]
    assert [(rows[i].epoch, rows[i].idx) for i in ids] == [(1, 1), (1, 2), (1, 3)]
    invalidated = await h.events_of(EventType.TRIAL_INVALIDATED)
    assert [(e.trial_id, e.payload["retry"], e.payload["next_trial_id"]) for e in invalidated] == [
        (ids[0], True, ids[1]),
        (ids[1], True, ids[2]),
    ]
    assert [t.validity for t in result.report.trials] == [
        Validity.INVALID,
        Validity.INVALID,
        Validity.VALID,
    ]


async def test_retries_stop_after_three_and_the_chain_ends_invalid(build) -> None:
    h = await build()
    ids = attempt_ids(5)
    for trial_id in ids:
        h.executor.raises[trial_id] = SandboxLost("still broken")

    await h.freeze_and_run()

    assert h.executor.calls == ids[:4]  # the original attempt plus three retries
    rows = {row.trial_id: row for row in await h.trials.list_by_batch(BATCH)}
    assert sorted(rows) == ids[:4]
    assert {row.state for row in rows.values()} == {"invalid"}
    invalidated = await h.events_of(EventType.TRIAL_INVALIDATED)
    assert [e.payload["retry"] for e in invalidated] == [True, True, True, False]
    assert invalidated[-1].payload["next_trial_id"] is None
    assert next_trial_id(ids[3]) == ids[4]  # the numbering would continue, the budget does not


async def test_provider_create_failure_counts_as_infra_failure(build) -> None:
    h = await build()
    h.providers["docker-a"].fail_next_creates(1, SandboxCreateError("image pull failed"))

    await h.freeze_and_run()

    ids = attempt_ids(2)
    assert h.executor.calls == [ids[1]]  # the first attempt never reached the executor
    rows = {row.trial_id: row for row in await h.trials.list_by_batch(BATCH)}
    assert (rows[ids[0]].state, rows[ids[1]].state) == ("invalid", "scored")
    assert h.scheduler.capacity("docker-a").in_use == 0


async def test_non_infrastructure_errors_are_not_retried(build) -> None:
    h = await build()
    h.executor.raises[TRIAL] = CapabilityNotSupported("gpu")

    await h.freeze_and_run()

    assert h.executor.calls == [TRIAL]
    rows = await h.trials.list_by_batch(BATCH)
    assert [(row.trial_id, row.state) for row in rows] == [(TRIAL, "invalid")]
    invalidated = (await h.events_of(EventType.TRIAL_INVALIDATED))[0]
    assert invalidated.payload["outcome"] == "invalid"
    assert invalidated.payload["retry"] is False


async def test_lost_host_requeues_its_trials_as_new_records_on_the_surviving_host(build) -> None:
    hosts = CapacitySpec("docker-a", hosts=(HostSpec("h1", slots=1), HostSpec("h2", slots=1)))
    h = await build(n_models=2, n_tasks=2, capacity=[hosts])
    h.executor.gate = asyncio.Event()
    h.driver.freeze(lint_passed=True, budget_approved=True)
    run = asyncio.create_task(h.driver.run())
    async with asyncio.timeout(5):
        await h.executor.arrived.get()
        await h.executor.arrived.get()  # one trial running on each host

    loss = await h.driver.host_lost("docker-a", "h1")
    h.executor.gate.set()
    result = await asyncio.wait_for(run, timeout=10)

    assert len(loss.trial_ids) == 1
    lost_id = loss.trial_ids[0]
    retried_id = next_trial_id(lost_id)
    rows = {row.trial_id: row for row in await h.trials.list_by_batch(BATCH)}
    assert rows[lost_id].state == "invalid"
    assert rows[retried_id].state == "scored"
    assert rows[retried_id].extensions["retry_of"] == lost_id
    invalidated = [
        e for e in await h.events_of(EventType.TRIAL_INVALIDATED) if e.trial_id == lost_id
    ]
    assert len(invalidated) == 1
    assert invalidated[0].payload["reason"].startswith("host lost")
    assert invalidated[0].payload["next_trial_id"] == retried_id
    drained = await h.events_of(EventType.HOST_DRAINED)
    assert [e.payload["trial_ids"] for e in drained] == [[lost_id]]
    assert result.host_losses == (loss,)
    retry_leases = await h.lease_rows.list_by_trial(retried_id)
    assert {lease.host_id for lease in retry_leases if lease.role == "agent"} == {"h2"}
    assert len([row for row in rows.values() if row.state == "scored"]) == 4
    assert all(s.destroyed for s in h.providers["docker-a"].created_sandboxes())
    assert h.executor.calls.count(lost_id) == 1


async def test_stale_hosts_are_drained_with_an_event(build) -> None:
    hosts = CapacitySpec("docker-a", hosts=(HostSpec("h1", slots=1), HostSpec("h2", slots=1)))
    h = await build(capacity=[hosts])

    h.clock.advance(61)
    losses = await h.driver.sweep_hosts()

    assert sorted(loss.host_id for loss in losses) == ["h1", "h2"]
    drained = await h.events_of(EventType.HOST_DRAINED)
    assert sorted(e.payload["host_id"] for e in drained) == ["h1", "h2"]


async def test_trial_without_any_remaining_capacity_is_invalid_not_retried(build) -> None:
    hosts = CapacitySpec("docker-a", hosts=(HostSpec("h1", slots=1),))
    h = await build(n_tasks=2, capacity=[hosts])
    h.executor.gate = asyncio.Event()
    h.driver.freeze(lint_passed=True, budget_approved=True)
    run = asyncio.create_task(h.driver.run())
    async with asyncio.timeout(5):
        running = await h.executor.arrived.get()

    await h.driver.host_lost("docker-a", "h1")
    result = await asyncio.wait_for(run, timeout=10)

    rows = {row.trial_id: row for row in await h.trials.list_by_batch(BATCH)}
    assert rows[running].state == "invalid"
    # The retry and the second task can never be hosted: invalid with a capacity reason.
    assert {row.state for row in rows.values()} == {"invalid"}
    reasons = [e.payload["reason"] for e in await h.events_of(EventType.TRIAL_INVALIDATED)]
    assert any(reason.startswith("CapacityUnavailable") for reason in reasons)
    assert result.state is BatchState.GRADED


async def test_sandboxes_are_destroyed_and_the_slot_freed_when_the_executor_crashes(
    build,
) -> None:
    h = await build()
    h.executor.raises[TRIAL] = RuntimeError("executor bug")

    with pytest.raises(ExceptionGroup) as crashed:
        await h.freeze_and_run()

    assert crashed.group_contains(RuntimeError, match="executor bug")
    assert h.scheduler.capacity("docker-a").in_use == 0
    sandboxes = h.providers["docker-a"].created_sandboxes()
    assert sandboxes
    assert all(sandbox.destroyed for sandbox in sandboxes)
    leases = await h.lease_rows.list_by_trial(TRIAL)
    assert leases
    assert all(lease.destroyed_at is not None for lease in leases)
