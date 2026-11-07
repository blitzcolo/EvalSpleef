"""Capacity semaphore: ceiling, CPU/memory bin-packing, per-host slots and host removal."""

from __future__ import annotations

from dataclasses import replace

import pytest

from evalspleef.control.scheduling import (
    CapacitySpec,
    HostSpec,
    InstanceCapacity,
    Reservation,
    ResourceRequest,
    capacity_spec,
)
from evalspleef.spi.caps import OSFamily
from evalspleef.testing.mock_sandbox import default_capabilities


def acquire(capacity: InstanceCapacity, trial_id: str, request: ResourceRequest) -> Reservation:
    reservation = capacity.try_acquire(trial_id, request)
    assert reservation is not None, f"{trial_id} should have fit"
    return reservation


@pytest.mark.parametrize(
    ("declared", "quota", "configured", "expected"),
    [
        (8, 5, 3, 3),
        (8, 2, 10, 2),
        (4, 9, 9, 4),
        (None, None, 7, 7),
        (None, None, None, None),
    ],
)
def test_ceiling_is_the_minimum_of_declared_quota_and_configured_limit(
    declared: int | None, quota: int | None, configured: int | None, expected: int | None
) -> None:
    caps = replace(default_capabilities(), max_concurrency=declared)
    preflight = {} if quota is None else {"quota": quota}
    limits = {} if configured is None else {"max_concurrency": configured}

    assert capacity_spec("a", caps, preflight, limits).concurrency_limit == expected


def test_semaphore_refuses_at_the_ceiling_and_frees_on_release() -> None:
    capacity = InstanceCapacity(CapacitySpec("a", configured_limit=3))
    held = [acquire(capacity, f"t{i}", ResourceRequest()) for i in range(3)]

    assert capacity.try_acquire("t3", ResourceRequest()) is None
    capacity.release(held[0])
    capacity.release(held[0])  # a second release must not free a second slot
    assert capacity.in_use == 2
    acquire(capacity, "t3", ResourceRequest())
    assert capacity.try_acquire("t4", ResourceRequest()) is None


def test_cpu_bin_packing_mixes_request_sizes() -> None:
    capacity = InstanceCapacity(CapacitySpec("c", cpus=4.0, memory_mb=65536))
    big = ResourceRequest(cpus=1.5, memory_mb=1024)
    acquire(capacity, "a", big)
    acquire(capacity, "b", big)

    assert capacity.try_acquire("c", big) is None  # 4.5 cpus do not fit into 4.0
    acquire(capacity, "d", ResourceRequest(cpus=1.0, memory_mb=1024))  # 4.0 exactly


def test_memory_bin_packing_binds_before_cpu() -> None:
    capacity = InstanceCapacity(CapacitySpec("c", cpus=64.0, memory_mb=8192))
    request = ResourceRequest(cpus=1.0, memory_mb=3000)
    acquire(capacity, "a", request)
    acquire(capacity, "b", request)

    assert capacity.try_acquire("c", request) is None


def test_macos_hosts_default_to_two_slots_and_unhealthy_hosts_are_dropped() -> None:
    caps = replace(
        default_capabilities(), os_families=frozenset({OSFamily.MACOS}), max_concurrency=None
    )
    spec = capacity_spec(
        "mac", caps, {"hosts": [{"host_id": "m1"}, {"host_id": "m2", "healthy": False}, "m3"]}, {}
    )

    assert [host.host_id for host in spec.hosts] == ["m1", "m3"]
    assert [host.slots for host in spec.hosts] == [2, 2]
    capacity = InstanceCapacity(spec)
    placed = [acquire(capacity, f"t{i}", ResourceRequest()).host_id for i in range(4)]
    assert sorted(placed) == ["m1", "m1", "m3", "m3"]
    assert capacity.try_acquire("t4", ResourceRequest()) is None


def test_linux_hosts_have_no_implicit_slot_limit() -> None:
    spec = capacity_spec("lin", default_capabilities(), {"hosts": ["h1"]}, {})

    assert spec.hosts == (HostSpec("h1"),)


def test_host_removal_marks_its_reservations_lost_and_keeps_the_rest_consistent() -> None:
    spec = CapacitySpec("a", hosts=(HostSpec("h1", slots=1), HostSpec("h2", slots=1)))
    capacity = InstanceCapacity(spec)
    first = acquire(capacity, "t1", ResourceRequest())
    second = acquire(capacity, "t2", ResourceRequest())
    assert (first.host_id, second.host_id) == ("h1", "h2")

    lost = capacity.remove_host("h1")

    assert lost == [first]
    assert first.lost.is_set()
    assert first.state == "lost"
    assert not second.lost.is_set()
    capacity.release(first)  # releasing a lost reservation is a no-op
    assert capacity.in_use == 1
    assert capacity.try_acquire("t3", ResourceRequest()) is None
    capacity.release(second)
    assert acquire(capacity, "t3", ResourceRequest()).host_id == "h2"
    assert capacity.available_hosts == ("h2",)


def test_max_parallel_combines_hosts_resources_and_the_ceiling() -> None:
    hosts = (HostSpec("h1", slots=2), HostSpec("h2", slots=2))
    request = ResourceRequest(cpus=1.5, memory_mb=1024)

    assert InstanceCapacity(CapacitySpec("a", hosts=hosts)).max_parallel(request) == 4
    assert (
        InstanceCapacity(CapacitySpec("a", configured_limit=3, hosts=hosts)).max_parallel(request)
        == 3
    )
    assert InstanceCapacity(CapacitySpec("a", cpus=4.0)).max_parallel(request) == 2
    assert InstanceCapacity(CapacitySpec("a")).max_parallel(request) is None


def test_malformed_limits_are_rejected_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        capacity_spec("a", default_capabilities(), {}, {"max_concurrency": "many"})
    with pytest.raises(ValueError, match="hosts"):
        capacity_spec("a", default_capabilities(), {"hosts": 7}, {})
