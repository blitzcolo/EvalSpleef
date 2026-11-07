from __future__ import annotations

import pytest

from evalspleef.hostd.hosts import HostRegistry
from evalspleef.spi.errors import QuotaExceeded, SandboxCreateError, SandboxLost
from evalspleef.testing.clock import FakeClock


def _registry() -> tuple[HostRegistry, FakeClock]:
    clock = FakeClock()
    registry = HostRegistry(clock)
    registry.register("h1", "127.0.0.1:1", slots={"docker": 2})
    registry.heartbeat("h1", mode="active", used={"docker": 0})
    return registry, clock


def test_host_is_lost_after_sixty_seconds_without_heartbeat() -> None:
    registry, clock = _registry()
    clock.advance(60)
    assert registry.sweep() == []
    assert registry.state("h1") == "active"
    clock.advance(0.5)
    assert registry.sweep() == ["h1"]
    assert registry.state("h1") == "lost"
    with pytest.raises(SandboxLost):
        registry.require_reachable("h1")
    with pytest.raises(SandboxCreateError):
        registry.require_active("h1")


def test_lost_listeners_get_the_trials_to_fail() -> None:
    registry, clock = _registry()
    registry.reserve_slot("h1", "docker", "trial-a")
    failed: list[tuple[str, frozenset[str]]] = []
    registry.add_lost_listener(lambda host, trials: failed.append((host, trials)))
    clock.advance(61)
    registry.sweep()
    registry.sweep()  # a host is reported lost once
    assert failed == [("h1", frozenset({"trial-a"}))]


def test_heartbeat_revives_a_lost_host_and_reports_its_mode() -> None:
    registry, clock = _registry()
    clock.advance(61)
    registry.sweep()
    registry.heartbeat("h1", mode="draining", used={"docker": 1})
    assert registry.state("h1") == "draining"
    with pytest.raises(SandboxCreateError):
        registry.reserve_slot("h1", "docker", "t")


def test_slot_accounting_follows_the_local_cap() -> None:
    registry, _ = _registry()
    registry.reserve_slot("h1", "docker", "a")
    registry.reserve_slot("h1", "docker", "b")
    assert registry.free_slots("h1", "docker") == 0
    with pytest.raises(QuotaExceeded):
        registry.reserve_slot("h1", "docker", "c")
    registry.release_slot("h1", "docker", "a")
    assert registry.free_slots("h1", "docker") == 1
