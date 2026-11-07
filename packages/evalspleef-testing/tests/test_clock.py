from __future__ import annotations

import asyncio

import pytest

from evalspleef.testing.clock import FakeClock, SystemClock


async def test_sleep_resolves_only_after_the_deadline_is_reached() -> None:
    clock = FakeClock(start=100.0)
    task = asyncio.create_task(clock.sleep(10))
    await asyncio.sleep(0)
    assert clock.pending_sleepers == 1

    clock.advance(9.5)
    await asyncio.sleep(0)
    assert not task.done()
    assert clock.now() == 109.5

    clock.advance(0.5)
    async with asyncio.timeout(2):
        await task
    assert clock.pending_sleepers == 0


async def test_one_advance_releases_every_due_sleeper_and_keeps_later_ones() -> None:
    clock = FakeClock()
    short = asyncio.create_task(clock.sleep(5))
    also_short = asyncio.create_task(clock.sleep(5))
    long = asyncio.create_task(clock.sleep(60))
    await asyncio.sleep(0)
    assert clock.pending_sleepers == 3

    clock.advance(5)
    async with asyncio.timeout(2):
        await asyncio.gather(short, also_short)
    assert not long.done()
    assert clock.pending_sleepers == 1
    long.cancel()


async def test_cancelled_sleeper_is_removed() -> None:
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(30))
    await asyncio.sleep(0)
    assert clock.pending_sleepers == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.pending_sleepers == 0


def test_clock_cannot_go_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        FakeClock().advance(-1)


async def test_system_clock_sleeps_and_reports_wall_time() -> None:
    clock = SystemClock()
    before = clock.now()
    await clock.sleep(0)
    assert clock.now() >= before
