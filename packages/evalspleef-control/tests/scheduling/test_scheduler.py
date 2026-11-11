"""Dispatch: ceiling under load, priority, fairness, starvation, host loss and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from evalspleef.control.scheduling import (
    CapacitySpec,
    CapacityUnavailable,
    HostSpec,
    Priority,
    Reservation,
    ResourceRequest,
    Scheduler,
    SchedulerAlert,
    Ticket,
    WorkItem,
)
from evalspleef.testing.clock import FakeClock


def item(
    trial_id: str,
    *,
    model: str = "alpha",
    routes: tuple[str, ...] = ("a",),
    priority: Priority = Priority.REGULAR,
    verifier_routes: tuple[str, ...] = (),
) -> WorkItem:
    return WorkItem(
        trial_id, f"cell-{trial_id}", model, routes, ResourceRequest(), priority, verifier_routes
    )


def one_slot(**kwargs: object) -> Scheduler:
    return Scheduler([CapacitySpec("a", configured_limit=1)], **kwargs)  # type: ignore[arg-type]


async def hold_the_only_slot(scheduler: Scheduler) -> Reservation:
    return await scheduler.acquire(item("held", model="held"))


def drain_order(scheduler: Scheduler, held: Reservation, tickets: dict[str, Ticket]) -> list[str]:
    """Release the slot repeatedly and record which waiter is granted next."""
    order: list[str] = []
    current = held
    while len(order) < len(tickets):
        scheduler.release(current)
        granted = [tid for tid, t in tickets.items() if t.future.done() and tid not in order]
        assert len(granted) == 1
        order.append(granted[0])
        current = tickets[granted[0]].future.result()
    return order


async def test_thirty_concurrent_trials_never_exceed_the_ceiling() -> None:
    scheduler = Scheduler(
        [CapacitySpec("a", declared_max_concurrency=8, preflight_quota=6, configured_limit=4)]
    )
    running = 0
    peak = 0

    async def trial(number: int) -> None:
        nonlocal running, peak
        reservation = await scheduler.acquire(item(f"t{number}", model=f"m{number % 3}"))
        running += 1
        peak = max(peak, running)
        assert running <= 4
        for _ in range(3):
            await asyncio.sleep(0)
        running -= 1
        scheduler.release(reservation)

    await asyncio.wait_for(asyncio.gather(*(trial(n) for n in range(30))), timeout=10)

    assert peak == 4
    assert scheduler.capacity("a").in_use == 0
    assert scheduler.pending == 0


async def test_a_grant_reserves_the_verifier_slot_next_to_the_agent() -> None:
    """Capacity counts sandboxes: a trial takes two slots, so one free slot is not a fit."""
    scheduler = Scheduler([CapacitySpec("a", configured_limit=2)])
    granted = await scheduler.acquire(item("t1", verifier_routes=("a",)))

    assert (granted.agent.instance, granted.verifier is not None) == ("a", True)
    assert granted.verifier is not None
    assert granted.verifier.instance == "a"
    assert scheduler.capacity("a").in_use == 2

    waiting = scheduler.enqueue(item("t2", verifier_routes=("a",)))
    scheduler.dispatch()
    assert not waiting.future.done()  # both slots are reserved by t1

    scheduler.release(granted)  # t2 is granted both slots in the same dispatch pass
    placed = await asyncio.wait_for(waiting.wait(), timeout=5)
    assert (placed.agent.instance, placed.verifier is not None) == ("a", True)
    scheduler.release(placed)
    assert scheduler.capacity("a").in_use == 0


async def test_the_verifier_reservation_falls_back_to_a_verifier_route() -> None:
    scheduler = Scheduler(
        [CapacitySpec("a", configured_limit=1), CapacitySpec("b", configured_limit=1)]
    )
    granted = await scheduler.acquire(item("t1", verifier_routes=("b",)))

    assert granted.agent.instance == "a"
    assert granted.verifier is not None
    assert granted.verifier.instance == "b"
    assert (scheduler.capacity("a").in_use, scheduler.capacity("b").in_use) == (1, 1)
    scheduler.release(granted)
    assert (scheduler.capacity("a").in_use, scheduler.capacity("b").in_use) == (0, 0)


async def test_a_trial_waits_when_only_the_agent_slot_is_free() -> None:
    """The agent reservation is not kept without the verifier's: no half trials."""
    scheduler = Scheduler(
        [CapacitySpec("a", configured_limit=3), CapacitySpec("b", configured_limit=1)]
    )
    filler = await scheduler.acquire(item("filler", verifier_routes=("b",)))  # a + the only b slot

    waiting = scheduler.enqueue(item("t", verifier_routes=("b",)))
    scheduler.dispatch()
    assert not waiting.future.done()
    # a still has two free slots, but the failed verifier placement released the agent.
    assert scheduler.capacity("a").in_use == 1

    scheduler.release(filler)
    placed = await asyncio.wait_for(waiting.wait(), timeout=5)
    assert placed.agent.instance == "a"
    assert placed.verifier is not None and placed.verifier.instance == "b"
    scheduler.release(placed)


async def test_first_route_instance_with_free_quota_wins() -> None:
    scheduler = Scheduler(
        [CapacitySpec("a", configured_limit=1), CapacitySpec("b", configured_limit=1)]
    )

    first = await scheduler.acquire(item("t1", routes=("a", "b")))
    second = await scheduler.acquire(item("t2", routes=("a", "b")))
    third = scheduler.enqueue(item("t3", routes=("a", "b")))
    scheduler.dispatch()

    assert (first.instance, second.instance) == ("a", "b")
    assert not third.future.done()


async def test_priority_order_is_niche_then_rerun_then_regular_then_control() -> None:
    scheduler = one_slot()
    held = await hold_the_only_slot(scheduler)
    tickets = {
        name: scheduler.enqueue(item(name, model=f"m-{name}", priority=priority))
        for name, priority in [
            ("regular", Priority.REGULAR),
            ("control", Priority.CONTROL),
            ("rerun", Priority.RERUN),
            ("niche", Priority.NICHE_PLATFORM),
        ]
    }
    scheduler.dispatch()

    assert drain_order(scheduler, held, tickets) == ["niche", "rerun", "regular", "control"]


async def test_models_are_served_round_robin_not_one_model_at_a_time() -> None:
    scheduler = one_slot()
    held = await hold_the_only_slot(scheduler)
    tickets = {
        f"{model}{number}": scheduler.enqueue(item(f"{model}{number}", model=model))
        for model in "ABC"
        for number in (1, 2, 3)
    }
    scheduler.dispatch()

    order = drain_order(scheduler, held, tickets)

    assert [name[0] for name in order] == list("ABCABCABC")
    assert [name for name in order if name[0] == "A"] == ["A1", "A2", "A3"]


async def test_starved_waiter_raises_alerts_once_and_jumps_the_queue() -> None:
    clock = FakeClock()
    seen: list[SchedulerAlert] = []
    scheduler = one_slot(clock=clock.now, on_alert=seen.append)
    held = await hold_the_only_slot(scheduler)
    old = scheduler.enqueue(item("old", model="m1", priority=Priority.REGULAR))

    clock.advance(30 * 60)
    assert [a.kind for a in scheduler.check_waiting()] == ["queue_timeout"]
    assert scheduler.check_waiting() == []
    clock.advance(90 * 60)
    assert [a.kind for a in scheduler.check_waiting()] == ["starvation"]
    newcomer = scheduler.enqueue(item("new", model="m2", priority=Priority.NICHE_PLATFORM))
    scheduler.dispatch()

    order = drain_order(scheduler, held, {"old": old, "new": newcomer})

    assert order == ["old", "new"]
    assert [(a.kind, a.trial_id) for a in seen] == [
        ("queue_timeout", "old"),
        ("starvation", "old"),
    ]
    assert scheduler.alerts == seen


async def test_lost_host_flags_its_trials_and_never_receives_new_work() -> None:
    spec = CapacitySpec("a", hosts=(HostSpec("h1", slots=1), HostSpec("h2", slots=1)))
    scheduler = Scheduler([spec])
    first = await scheduler.acquire(item("t1"))
    second = await scheduler.acquire(item("t2"))
    on_h1, on_h2 = (first, second) if first.host_id == "h1" else (second, first)

    loss = scheduler.host_lost("a", "h1")
    waiting = scheduler.enqueue(item("t3"))
    scheduler.dispatch()

    assert loss.trial_ids == (on_h1.trial_id,)
    assert on_h1.agent.lost.is_set()
    assert not on_h2.agent.lost.is_set()
    assert not waiting.future.done()
    scheduler.release(on_h2)
    granted = await asyncio.wait_for(waiting.wait(), timeout=5)
    assert granted.host_id == "h2"


async def test_waiter_fails_fast_when_no_capacity_can_ever_host_it() -> None:
    scheduler = Scheduler([CapacitySpec("a", hosts=(HostSpec("h1", slots=1),))])
    await scheduler.acquire(item("t1"))
    waiting = scheduler.enqueue(item("t2"))

    scheduler.host_lost("a", "h1")

    with pytest.raises(CapacityUnavailable):
        await asyncio.wait_for(waiting.wait(), timeout=5)


async def test_stale_heartbeat_drains_only_the_silent_host() -> None:
    clock = FakeClock()
    spec = CapacitySpec("a", hosts=(HostSpec("h1", slots=1), HostSpec("h2", slots=1)))
    scheduler = Scheduler([spec], clock=clock.now)

    clock.advance(50)
    scheduler.heartbeat("a", "h2")
    clock.advance(20)  # h1 silent for 70 s, h2 for 20 s
    losses = scheduler.sweep_stale_hosts()

    assert [(loss.instance, loss.host_id) for loss in losses] == [("a", "h1")]
    assert scheduler.capacity("a").available_hosts == ("h2",)
    assert scheduler.sweep_stale_hosts() == []


async def test_cancelled_waiter_leaves_the_queue_without_leaking_a_slot() -> None:
    scheduler = one_slot()
    held = await hold_the_only_slot(scheduler)
    waiter = asyncio.create_task(scheduler.acquire(item("waiting")))
    await asyncio.sleep(0)  # let the task enqueue itself
    assert scheduler.pending == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    scheduler.release(held)

    assert scheduler.pending == 0
    await asyncio.wait_for(scheduler.acquire(item("next")), timeout=5)
    assert scheduler.capacity("a").in_use == 1


async def test_cancelling_a_granted_ticket_returns_its_slot() -> None:
    scheduler = one_slot()
    ticket = scheduler.enqueue(item("t1"))
    scheduler.dispatch()
    assert scheduler.capacity("a").in_use == 1

    scheduler.cancel(ticket)

    assert scheduler.capacity("a").in_use == 0
