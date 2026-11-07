"""Per-provider-instance capacity accounting (TECH-DESIGN section 7.2, docs/02 section 9.2).

Every provider instance owns one :class:`InstanceCapacity`, which behaves like a counting
semaphore with placement: the concurrency ceiling is the minimum of the declared
``max_concurrency``, the vendor quota reported by ``preflight`` and the manual limit from
``providers.yaml``. Container providers additionally bin-pack CPU and memory, and hosted
providers count slots per host (macOS VMs: two per physical host).

The class is synchronous and never blocks: the :class:`~.scheduler.Scheduler` owns the waiting
queue, so fairness and priority are decided in one place.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from evalspleef.spi.caps import OSFamily
from evalspleef.spi.sandbox import SandboxCapabilities

__all__ = [
    "MACOS_SLOTS_PER_HOST",
    "CapacitySpec",
    "HostSpec",
    "InstanceCapacity",
    "Reservation",
    "ResourceRequest",
    "capacity_spec",
]

#: Apple's virtualization licence allows two VMs per physical Mac (TECH-DESIGN section 7.2).
MACOS_SLOTS_PER_HOST = 2

_EPSILON = 1e-9


@dataclass(frozen=True)
class ResourceRequest:
    """What one trial takes from an instance; zero means "not bin-packed"."""

    cpus: float = 0.0
    memory_mb: int = 0


@dataclass(frozen=True)
class HostSpec:
    """One physical or virtual host of an instance. ``None`` means unbounded on that axis."""

    host_id: str
    slots: int | None = None
    cpus: float | None = None
    memory_mb: int | None = None


@dataclass(frozen=True)
class CapacitySpec:
    """Inputs of the capacity semaphore of one instance."""

    instance: str
    declared_max_concurrency: int | None = None
    preflight_quota: int | None = None
    configured_limit: int | None = None
    cpus: float | None = None
    """Pool-wide CPU budget for instances without a host inventory."""
    memory_mb: int | None = None
    hosts: tuple[HostSpec, ...] = ()

    @property
    def concurrency_limit(self) -> int | None:
        """Minimum of the declared, preflight and configured limits; ``None`` if all unset."""
        limits = [
            limit
            for limit in (
                self.declared_max_concurrency,
                self.preflight_quota,
                self.configured_limit,
            )
            if limit is not None
        ]
        return min(limits) if limits else None


@dataclass(eq=False)
class Reservation:
    """A granted slot. ``lost`` is set when the host disappears under a running trial."""

    reservation_id: str
    instance: str
    host_id: str | None
    trial_id: str
    request: ResourceRequest
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "active"
    """``active`` -> ``released`` or ``lost``."""


@dataclass
class _HostState:
    host_id: str | None
    slots: int | None
    cpus: float | None
    memory_mb: int | None
    used_slots: int = 0
    used_cpus: float = 0.0
    used_memory_mb: int = 0
    available: bool = True

    def fits(self, request: ResourceRequest) -> bool:
        if not self.available:
            return False
        if self.slots is not None and self.used_slots >= self.slots:
            return False
        if self.cpus is not None and self.used_cpus + request.cpus > self.cpus + _EPSILON:
            return False
        return not (
            self.memory_mb is not None and self.used_memory_mb + request.memory_mb > self.memory_mb
        )

    def capacity_for(self, request: ResourceRequest) -> int | None:
        """How many requests of this size fit on the empty host; ``None`` if unbounded."""
        if not self.available:
            return 0
        counts: list[int] = []
        if self.slots is not None:
            counts.append(self.slots)
        if self.cpus is not None and request.cpus > 0:
            counts.append(int((self.cpus + _EPSILON) // request.cpus))
        if self.memory_mb is not None and request.memory_mb > 0:
            counts.append(self.memory_mb // request.memory_mb)
        return min(counts) if counts else None

    def leftover(self, request: ResourceRequest) -> tuple[float, float, float, str]:
        """Best-fit key: the tightest host after placement wins, ties broken by host id."""
        inf = float("inf")
        return (
            inf if self.slots is None else self.slots - self.used_slots - 1,
            inf if self.cpus is None else self.cpus - self.used_cpus - request.cpus,
            inf
            if self.memory_mb is None
            else self.memory_mb - self.used_memory_mb - request.memory_mb,
            self.host_id or "",
        )


class InstanceCapacity:
    """Semaphore with placement for one provider instance."""

    def __init__(self, spec: CapacitySpec) -> None:
        self.spec = spec
        self.instance = spec.instance
        if spec.hosts:
            self._states: dict[str | None, _HostState] = {
                host.host_id: _HostState(host.host_id, host.slots, host.cpus, host.memory_mb)
                for host in spec.hosts
            }
        else:
            self._states = {None: _HostState(None, None, spec.cpus, spec.memory_mb)}
        self._active: dict[str, Reservation] = {}
        self._ids = itertools.count(1)

    @property
    def in_use(self) -> int:
        return len(self._active)

    @property
    def available_hosts(self) -> tuple[str, ...]:
        return tuple(
            host_id for host_id, state in self._states.items() if host_id and state.available
        )

    def has_host(self, host_id: str) -> bool:
        return host_id in self._states

    def max_parallel(self, request: ResourceRequest) -> int | None:
        """Trials of this size that can run at once on the empty instance; ``None`` if unbounded."""
        counts = [state.capacity_for(request) for state in self._states.values()]
        total = None if any(c is None for c in counts) else sum(c for c in counts if c is not None)
        limit = self.spec.concurrency_limit
        if limit is None:
            return total
        return limit if total is None else min(limit, total)

    def can_ever_fit(self, request: ResourceRequest) -> bool:
        """Whether an empty instance could host ``request`` (false once every host is lost)."""
        parallel = self.max_parallel(request)
        return parallel is None or parallel >= 1

    def try_acquire(self, trial_id: str, request: ResourceRequest) -> Reservation | None:
        """Take a slot if the ceiling, a host slot and the CPU/memory budget all allow it."""
        limit = self.spec.concurrency_limit
        if limit is not None and self.in_use >= limit:
            return None
        candidates = [state for state in self._states.values() if state.fits(request)]
        if not candidates:
            return None
        chosen = min(candidates, key=lambda state: state.leftover(request))
        chosen.used_slots += 1
        chosen.used_cpus += request.cpus
        chosen.used_memory_mb += request.memory_mb
        reservation = Reservation(
            reservation_id=f"{self.instance}#{next(self._ids)}",
            instance=self.instance,
            host_id=chosen.host_id,
            trial_id=trial_id,
            request=request,
        )
        self._active[reservation.reservation_id] = reservation
        return reservation

    def release(self, reservation: Reservation) -> None:
        """Return a slot; releasing twice, or after the host was lost, is a no-op."""
        if reservation.state != "active":
            return
        reservation.state = "released"
        self._active.pop(reservation.reservation_id, None)
        state = self._states.get(reservation.host_id)
        if state is None:
            return
        state.used_slots -= 1
        state.used_cpus -= reservation.request.cpus
        state.used_memory_mb -= reservation.request.memory_mb

    def remove_host(self, host_id: str) -> list[Reservation]:
        """Drop a host and mark every reservation on it lost; returns the affected ones."""
        state = self._states.get(host_id)
        if state is None:
            return []
        state.available = False
        affected = [r for r in self._active.values() if r.host_id == host_id]
        for reservation in affected:
            reservation.state = "lost"
            self._active.pop(reservation.reservation_id, None)
            reservation.lost.set()
        state.used_slots = 0
        state.used_cpus = 0.0
        state.used_memory_mb = 0
        return affected


def capacity_spec(
    instance: str,
    capabilities: SandboxCapabilities,
    preflight: Mapping[str, Any],
    limits: Mapping[str, object],
) -> CapacitySpec:
    """Build a :class:`CapacitySpec` from the three sources of TECH-DESIGN section 7.2.

    ``preflight`` and ``limits`` come from outside the process, so they are validated here.
    Recognised keys: ``preflight["quota"]`` (int), ``preflight["hosts"]`` (host inventory, each
    entry a host id or a mapping with ``host_id``, ``healthy``, ``slots``, ``cpus``,
    ``memory_mb``); ``limits["max_concurrency"]``, ``limits["cpus"]``, ``limits["memory_mb"]``,
    ``limits["slots_per_host"]`` and ``limits["hosts"]`` (same shape as the preflight inventory).
    Without an explicit ``slots_per_host``, macOS-capable instances get two slots per host.
    """
    default_slots = _opt_int(limits.get("slots_per_host"), "limits.slots_per_host")
    if default_slots is None and OSFamily.MACOS in capabilities.os_families:
        default_slots = MACOS_SLOTS_PER_HOST
    raw_hosts = preflight.get("hosts")
    if raw_hosts is None:
        raw_hosts = limits.get("hosts")
    return CapacitySpec(
        instance=instance,
        declared_max_concurrency=capabilities.max_concurrency,
        preflight_quota=_opt_int(preflight.get("quota"), "preflight.quota"),
        configured_limit=_opt_int(limits.get("max_concurrency"), "limits.max_concurrency"),
        cpus=_opt_float(limits.get("cpus"), "limits.cpus"),
        memory_mb=_opt_int(limits.get("memory_mb"), "limits.memory_mb"),
        hosts=_parse_hosts(raw_hosts, default_slots),
    )


def _opt_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return value


def _opt_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, got {value!r}")
    return float(value)


def _parse_hosts(raw: object, default_slots: int | None) -> tuple[HostSpec, ...]:
    if raw is None:
        return ()
    entries: list[tuple[str, Mapping[str, object]]] = []
    if isinstance(raw, Mapping):
        for host_id, body in raw.items():
            entries.append((str(host_id), body if isinstance(body, Mapping) else {}))
    elif isinstance(raw, list | tuple):
        for item in raw:
            if isinstance(item, Mapping):
                entries.append((str(item["host_id"]), item))
            else:
                entries.append((str(item), {}))
    else:
        raise ValueError(f"hosts must be a list or a mapping, got {type(raw).__name__}")
    hosts: list[HostSpec] = []
    for host_id, body in entries:
        if body.get("healthy", True) is False:
            continue
        slots = _opt_int(body.get("slots"), f"hosts[{host_id}].slots")
        hosts.append(
            HostSpec(
                host_id=host_id,
                slots=slots if slots is not None else default_slots,
                cpus=_opt_float(body.get("cpus"), f"hosts[{host_id}].cpus"),
                memory_mb=_opt_int(body.get("memory_mb"), f"hosts[{host_id}].memory_mb"),
            )
        )
    return tuple(hosts)
