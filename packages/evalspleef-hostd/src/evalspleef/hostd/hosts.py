"""Host inventory on the control-plane side (TECH-DESIGN section 7.5, PRD FR-SBX-10).

States: ``active``, ``draining`` (no new leases), ``maintenance`` (no calls at all) and ``lost``
(no heartbeat for 60 s). The mode a daemon reports in its heartbeat is authoritative; ``lost``
is derived from silence and clears by itself when heartbeats resume. A lost host's trials are
failed as environment errors by the callers listening to ``add_lost_listener``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from evalspleef.spi.errors import QuotaExceeded, SandboxCreateError, SandboxLost

from .clock import Clock, SystemClock

HostState = Literal["active", "draining", "lost", "maintenance"]
HostMode = Literal["active", "draining", "maintenance"]

HEARTBEAT_INTERVAL_S = 15.0
HEARTBEAT_LOSS_S = 60.0


@dataclass
class ProviderSlots:
    total: int | None
    """Local concurrency cap of the daemon; ``None`` means the daemon enforces no cap."""
    used: int = 0
    leased: int = 0
    """Slots the control plane reserved for creates that have not shown up in a heartbeat yet."""


@dataclass(frozen=True)
class ReclaimedRecord:
    provider: str
    sandbox_id: str
    trial_id: str
    reason: str


@dataclass
class HostRecord:
    host_id: str
    service_address: str
    info: dict[str, Any]
    registered_at: float
    last_heartbeat: float
    mode: HostMode = "active"
    lost: bool = False
    slots: dict[str, ProviderSlots] = field(default_factory=dict)
    disk_free_mb: int = 0
    load_1m: float = 0.0
    hostd_version: str = ""
    trials: dict[str, str] = field(default_factory=dict)
    """trial_id -> provider name, for the trials running on this host."""
    reclaimed: list[ReclaimedRecord] = field(default_factory=list)

    @property
    def state(self) -> HostState:
        return "lost" if self.lost else self.mode


LostListener = Callable[[str, frozenset[str]], None]


class HostRegistry:
    """Inventory, slot accounting and liveness of registered hosts."""

    def __init__(
        self, clock: Clock | None = None, *, loss_after_s: float = HEARTBEAT_LOSS_S
    ) -> None:
        self._clock = clock or SystemClock()
        self._loss_after_s = loss_after_s
        self._hosts: dict[str, HostRecord] = {}
        self._listeners: list[LostListener] = []

    # -- membership ---------------------------------------------------------------

    def register(
        self,
        host_id: str,
        service_address: str,
        *,
        info: dict[str, Any] | None = None,
        slots: dict[str, int | None] | None = None,
        hostd_version: str = "",
    ) -> HostRecord:
        now = self._clock.now()
        previous = self._hosts.get(host_id)
        record = HostRecord(
            host_id=host_id,
            service_address=service_address,
            info=dict(info or {}),
            registered_at=now,
            last_heartbeat=now,
            slots={name: ProviderSlots(total=total) for name, total in (slots or {}).items()},
            hostd_version=hostd_version,
        )
        if previous is not None:
            record.trials = previous.trials
            record.reclaimed = previous.reclaimed
        self._hosts[host_id] = record
        return record

    def get(self, host_id: str) -> HostRecord:
        try:
            return self._hosts[host_id]
        except KeyError:
            raise SandboxLost(f"unknown host {host_id!r}") from None

    def inventory(self) -> list[HostRecord]:
        return list(self._hosts.values())

    def state(self, host_id: str) -> HostState:
        return self.get(host_id).state

    # -- liveness -----------------------------------------------------------------

    def heartbeat(
        self,
        host_id: str,
        *,
        mode: HostMode,
        used: dict[str, int],
        totals: dict[str, int | None] | None = None,
        disk_free_mb: int = 0,
        load_1m: float = 0.0,
    ) -> HostRecord:
        record = self.get(host_id)
        record.last_heartbeat = self._clock.now()
        record.lost = False
        record.mode = mode
        record.disk_free_mb = disk_free_mb
        record.load_1m = load_1m
        for name, count in used.items():
            slots = record.slots.setdefault(name, ProviderSlots(total=None))
            slots.used = count
            if totals is not None and name in totals:
                slots.total = totals[name]
        return record

    def add_lost_listener(self, listener: LostListener) -> None:
        """Called with (host_id, trial ids) when a host turns lost: fail those trials."""
        self._listeners.append(listener)

    def sweep(self) -> list[str]:
        """Mark hosts silent for more than the loss window as lost; returns the new ones."""
        now = self._clock.now()
        newly_lost: list[str] = []
        for record in self._hosts.values():
            if not record.lost and now - record.last_heartbeat > self._loss_after_s:
                record.lost = True
                newly_lost.append(record.host_id)
                trials = frozenset(record.trials)
                for listener in self._listeners:
                    listener(record.host_id, trials)
        return newly_lost

    # -- what callers may do with a host -------------------------------------------

    def require_active(self, host_id: str) -> HostRecord:
        """Gate for new leases."""
        record = self.get(host_id)
        if record.state != "active":
            raise SandboxCreateError(f"host {host_id} is {record.state}, not accepting new leases")
        return record

    def require_reachable(self, host_id: str) -> HostRecord:
        """Gate for calls on existing sandboxes: a lost host fails them as environment errors."""
        record = self.get(host_id)
        if record.lost:
            raise SandboxLost(f"host {host_id} lost: no heartbeat for over {self._loss_after_s:g}s")
        return record

    # -- slots and trials -----------------------------------------------------------

    def free_slots(self, host_id: str, provider: str) -> int | None:
        slots = self.get(host_id).slots.get(provider)
        if slots is None or slots.total is None:
            return None
        return max(0, slots.total - max(slots.used, slots.leased))

    def reserve_slot(self, host_id: str, provider: str, trial_id: str) -> None:
        record = self.require_active(host_id)
        slots = record.slots.setdefault(provider, ProviderSlots(total=None))
        if slots.total is not None and max(slots.used, slots.leased) >= slots.total:
            raise QuotaExceeded(f"host {host_id} has no free {provider} slot")
        slots.leased += 1
        record.trials[trial_id] = provider

    def release_slot(self, host_id: str, provider: str, trial_id: str) -> None:
        record = self.get(host_id)
        if record.trials.pop(trial_id, None) is not None:
            slots = record.slots.get(provider)
            if slots is not None and slots.leased > 0:
                slots.leased -= 1

    def record_reclaimed(self, host_id: str, items: list[ReclaimedRecord]) -> None:
        self.get(host_id).reclaimed.extend(items)
