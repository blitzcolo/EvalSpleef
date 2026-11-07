"""Warm-up, batch guards, retirement, the health loop and the idle-spin alert."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest

from evalspleef.control.endpoints import (
    EndpointManagerConfig,
    HealthLoop,
    IdleSpinAlert,
    InvalidTransition,
    TicketError,
    public_name,
    required_replicas,
)
from evalspleef.spi.model import EndpointState, ModelSpec

MakeWorld = Callable[..., Awaitable[Any]]
SpecFactory = Callable[..., ModelSpec]

API_BASE = "https://models.example.test/v1"
MODEL_ID = "qwen-32b"
HF_TOKEN_REF = "vault://hf/eval"
BOUNDED_S = 10.0


async def ready_endpoint(world: Any, spec: ModelSpec, *, attach: bool = False) -> str:
    """Register an endpoint and drive it to READY through the mode's normal path."""
    handle = await world.manager.register(spec)
    if attach:
        await world.manager.attach(
            handle.endpoint_id, {"base_url": API_BASE, "model": MODEL_ID, "deployment_id": "dep-1"}
        )
    else:
        await world.manager.probe(handle.endpoint_id)
    assert (await world.manager.get(handle.endpoint_id)).state is EndpointState.READY
    return str(handle.endpoint_id)


# -- replica arithmetic ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peak", "per_trial", "per_replica", "expected"),
    [(10, 3, 16, 2), (32, 1, 16, 2), (33, 1, 16, 3), (1, 1, 16, 1)],
)
def test_required_replicas_rounds_up(
    peak: int, per_trial: int, per_replica: int, expected: int
) -> None:
    assert (
        required_replicas(
            peak_concurrent_trials=peak,
            requests_per_trial=per_trial,
            max_concurrency_per_replica=per_replica,
        )
        == expected
    )


async def test_replicas_required_uses_the_planned_concurrency_per_replica(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api")
    endpoint_id = await ready_endpoint(world, spec_factory(params={"max_concurrency": 8}))

    needed = await world.manager.replicas_required(
        endpoint_id, peak_concurrent_trials=10, requests_per_trial=3
    )

    assert needed == 4  # ceil(10 * 3 / 8)


# -- warm-up ---------------------------------------------------------------------------------


async def test_warm_scales_the_deployment_and_reaches_warm(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api")
    endpoint_id = await ready_endpoint(world, spec_factory())

    result = (await world.manager.warm([endpoint_id], 3))[endpoint_id]

    assert result.state is EndpointState.WARM
    assert (result.replicas_required, result.replicas_confirmed, result.cold_start_s) == (
        3,
        True,
        None,
    )
    row = await world.endpoints.get(endpoint_id)
    assert row is not None and row.observed["min_replicas"] == 3  # the provider was asked to scale
    assert (await world.transitions())[-1] == ("endpoint.warm", "ready", "warm")
    assert (await world.manager.running_guard([endpoint_id])).ok
    await world.assert_row_matches_log(endpoint_id)


async def test_cold_start_seen_by_the_gateway_is_recorded_as_an_event(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api", provider_params={"cold_start_window_s": 30})
    endpoint_id = await ready_endpoint(world, spec_factory())
    world.server.inject_error(503, count=2)  # the deployment is still waking up

    result = (await world.manager.warm([endpoint_id], 1))[endpoint_id]

    assert result.state is EndpointState.WARM
    assert result.cold_start_s is not None and result.cold_start_s > 0
    events = await world.events_in()
    cold = [e for e in events if str(e.type) == "endpoint.cold_start"]
    assert len(cold) == 1
    assert cold[0].payload["cold_start_s"] == result.cold_start_s
    assert cold[0].payload["source"] == "wake"
    assert [str(e.type) for e in events[-2:]] == ["endpoint.cold_start", "endpoint.warm"]


async def test_wake_timeout_leaves_the_endpoint_sleeping_and_blocks_running(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api", provider_params={"cold_start_s": 1000})
    endpoint_id = await ready_endpoint(world, spec_factory())

    result = (await world.manager.warm([endpoint_id], 1))[endpoint_id]

    assert result.state is EndpointState.SLEEPING
    last = (await world.events_in())[-1]
    assert (str(last.type), last.payload["reason"]) == ("endpoint.unavailable", "wake_timeout")
    guard = await world.manager.running_guard([endpoint_id])
    assert not guard.ok and "sleeping" in guard.blockers[0]


async def test_assisted_warm_waits_for_the_operator_to_confirm_the_replicas(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    endpoint_id = await ready_endpoint(world, spec_factory(), attach=True)

    result = (await world.manager.warm([endpoint_id], 2))[endpoint_id]

    assert result.state is EndpointState.WARM and not result.replicas_confirmed
    guard = await world.manager.running_guard([endpoint_id])
    assert not guard.ok and "not confirmed" in guard.blockers[0]
    row = await world.endpoints.get(endpoint_id)
    assert row is not None and row.observed["pending_scale"] == {
        "min_replicas": 2,
        "max_replicas": 3,
    }

    assert result.ticket_id is not None
    await world.manager.confirm_ticket(result.ticket_id, "alice")
    assert (await world.manager.running_guard([endpoint_id])).ok
    with pytest.raises(TicketError):
        await world.manager.confirm_ticket(result.ticket_id, "alice")  # already closed


async def test_deploy_tickets_are_closed_by_attach_not_by_confirmation(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    handle = await world.manager.register(spec_factory())
    (ticket,) = await world.manager.tickets(handle.endpoint_id)

    with pytest.raises(TicketError):
        await world.manager.confirm_ticket(ticket.ticket_id, "alice")
    with pytest.raises(TicketError):
        await world.manager.confirm_ticket("tkt_missing", "alice")


# -- freeze guard ----------------------------------------------------------------------------


async def test_freeze_guard_requires_ready_endpoints_and_a_token_valid_past_plan_plus_7_days(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    expiry: dict[str, date | None] = {HF_TOKEN_REF: date(2027, 1, 7)}
    world = await make_world(token_expiry=expiry)
    planned_end = date(2026, 12, 31)  # + 7 days = 2027-01-07
    handle = await world.manager.register(spec_factory(hf=True))
    endpoint_id = handle.endpoint_id

    awaiting = await world.manager.freeze_guard([endpoint_id], planned_end=planned_end)
    assert not awaiting.ok and "awaiting_operator" in awaiting.blockers[0]

    await world.manager.attach(
        endpoint_id, {"base_url": API_BASE, "model": MODEL_ID, "deployment_id": "dep-1"}
    )
    # Expiry exactly at planned end + 7 days is not enough; a day later it is.
    boundary = await world.manager.freeze_guard([endpoint_id], planned_end=planned_end)
    assert not boundary.ok and HF_TOKEN_REF in boundary.blockers[0]
    expiry[HF_TOKEN_REF] = date(2027, 1, 8)
    assert (await world.manager.freeze_guard([endpoint_id], planned_end=planned_end)).ok
    expiry[HF_TOKEN_REF] = None  # an unrecorded expiry cannot be verified
    unknown = await world.manager.freeze_guard([endpoint_id], planned_end=planned_end)
    assert not unknown.ok


async def test_freeze_guard_has_no_token_check_for_vendor_sources(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="external")
    endpoint_id = await ready_endpoint(world, spec_factory())

    assert (await world.manager.freeze_guard([endpoint_id], planned_end=date(2026, 12, 31))).ok


# -- retirement ------------------------------------------------------------------------------


async def test_api_retire_sleeps_then_terminates_and_drops_the_route(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api")
    endpoint_id = await ready_endpoint(world, spec_factory())
    name = public_name(endpoint_id)

    slept = await world.manager.retire(endpoint_id, "sleep")
    assert slept.state is EndpointState.SLEEPING and name in world.admin.routes
    last = (await world.events_in())[-1]
    assert (str(last.type), last.payload["policy"]) == ("endpoint.retired", "sleep")

    gone = await world.manager.retire(endpoint_id, "terminate")
    assert gone.state is EndpointState.RETIRED and name not in world.admin.routes
    row = await world.endpoints.get(endpoint_id)
    assert row is not None and row.retired_at is not None
    with pytest.raises(InvalidTransition):
        await world.manager.retire(endpoint_id, "terminate")
    await world.assert_row_matches_log(endpoint_id)


async def test_external_endpoints_are_only_deregistered(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="external")
    endpoint_id = await ready_endpoint(world, spec_factory())

    retired = await world.manager.retire(endpoint_id, "sleep")

    assert retired.state is EndpointState.RETIRED
    assert public_name(endpoint_id) not in world.admin.routes


async def test_assisted_terminate_is_confirmed_by_the_endpoint_going_away(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    endpoint_id = await ready_endpoint(world, spec_factory(), attach=True)

    pending = await world.manager.retire(endpoint_id, "terminate")

    assert pending.state is EndpointState.READY  # nothing happens until the operator acts
    ticket = next(t for t in await world.manager.tickets(endpoint_id) if t.kind == "teardown")
    assert ticket.status == "open"
    with pytest.raises(TicketError):  # the deployment still answers: termination not applied
        await world.manager.confirm_ticket(ticket.ticket_id, "alice")
    assert (await world.manager.get(endpoint_id)).state is EndpointState.READY

    world.server.inject_error(503)  # the deployment is gone
    retired = await world.manager.confirm_ticket(ticket.ticket_id, "alice")

    assert retired.state is EndpointState.RETIRED
    assert public_name(endpoint_id) not in world.admin.routes
    assert (await world.manager.tickets(endpoint_id))[-1].status == "closed"


async def test_assisted_sleep_confirmation_sends_no_request_that_could_wake_the_deployment(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    endpoint_id = await ready_endpoint(world, spec_factory(), attach=True)
    await world.manager.retire(endpoint_id, "sleep")
    ticket = next(t for t in await world.manager.tickets(endpoint_id) if t.kind == "teardown")
    requests_before = len(world.server.requests)

    slept = await world.manager.confirm_ticket(ticket.ticket_id, "alice")

    assert slept.state is EndpointState.SLEEPING
    assert len(world.server.requests) == requests_before


# -- health loop and idle spin ------------------------------------------------------------------


async def test_health_loop_pings_only_while_a_batch_runs_and_tracks_degradation(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api")
    endpoint_id = await ready_endpoint(world, spec_factory())
    await world.manager.warm([endpoint_id], 1)
    batches: dict[str, list[str]] = {}

    async def running() -> dict[str, list[str]]:
        return batches

    loop = HealthLoop(world.manager, running, clock=world.clock)

    before = len(world.server.requests)
    assert await loop.tick() == {}
    assert len(world.server.requests) == before  # no batch Running: no health traffic, no cost

    batches["b1"] = [endpoint_id]
    pings = await loop.tick()
    assert pings[endpoint_id].ok
    assert len(world.server.requests) == before + 1
    assert world.server.requests[-1].body["max_tokens"] == 1
    assert await world.events_in("b1") == []  # a healthy WARM endpoint produces no event

    world.server.inject_error(500)
    await loop.tick()
    degraded = (await world.events_in("b1"))[-1]
    assert (str(degraded.type), degraded.payload["state"]) == ("endpoint.unavailable", "degraded")
    assert degraded.payload["status"] == 500

    await loop.tick()
    recovered = (await world.events_in("b1"))[-1]
    assert (str(recovered.type), recovered.payload["from_state"]) == ("endpoint.warm", "degraded")
    assert (await world.manager.get(endpoint_id)).state is EndpointState.WARM


async def test_health_loop_ticks_every_interval_of_clock_time_until_stopped(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="api")
    endpoint_id = await ready_endpoint(world, spec_factory())
    await world.manager.warm([endpoint_id], 1)
    batches = {"b1": [endpoint_id]}

    async def running() -> dict[str, list[str]]:
        return batches

    loop = HealthLoop(world.manager, running, clock=world.clock, interval_s=60.0)
    world.server.inject_error(500)  # tick 1 degrades the endpoint, tick 2 recovers it
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))

    await asyncio.wait_for(world.clock.sleeping.wait(), BOUNDED_S)  # tick 1 done, now sleeping
    assert [e.payload["state"] for e in await world.events_in("b1")] == ["degraded"]

    world.clock.sleeping.clear()
    world.clock.advance(59)  # not yet one interval
    assert not world.clock.sleeping.is_set()
    world.clock.advance(1)
    await asyncio.wait_for(world.clock.sleeping.wait(), BOUNDED_S)  # tick 2 done
    assert [e.payload["state"] for e in await world.events_in("b1")] == ["degraded", "warm"]

    stop.set()
    await asyncio.wait_for(task, BOUNDED_S)


async def test_idle_warm_endpoint_raises_one_alert_until_traffic_resumes(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(
        mode="api", config=EndpointManagerConfig(warm_ping_timeout_s=30.0, idle_threshold_s=900.0)
    )
    endpoint_id = await ready_endpoint(world, spec_factory())
    await world.manager.warm([endpoint_id], 1)

    assert await world.manager.check_idle() == []
    world.clock.advance(899)
    assert await world.manager.check_idle() == []
    world.clock.advance(1)
    assert await world.manager.check_idle() == [IdleSpinAlert(endpoint_id, 900.0)]
    assert world.idle_alerts == [IdleSpinAlert(endpoint_id, 900.0)]
    assert await world.manager.check_idle() == []  # one alert per idle period

    await world.manager.record_traffic(endpoint_id)
    world.clock.advance(900)
    assert await world.manager.check_idle() == [IdleSpinAlert(endpoint_id, 900.0)]
