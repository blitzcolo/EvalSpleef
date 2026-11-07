"""Dynamic dispatch: queueing, fairness, priority and host loss (docs/02 section 9, PRD FR-FLOW-03).

Work items wait in one queue. A dispatch pass grants slots in this order:

1. priority (:class:`Priority`): scarce platforms first, then reruns, regular Linux trials,
   and control runs last, as numbered in docs/02 section 9.1;
2. fairness: among equal priorities the model that has been served the fewest slots goes first,
   so every model's tasks interleave instead of one model finishing its easy tasks early;
3. arrival order.

The first route instance with free capacity wins (TECH-DESIGN section 7.2). Running trials are
never preempted; the only way a reservation ends early is a lost host, which sets
``Reservation.lost`` so the caller can fail the trial as ``failed_infra`` and retry it as a new
record (see :mod:`.retry`). A waiter older than the starvation threshold jumps the queue and
raises an alert.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from evalspleef.spi.caps import OSFamily, RuntimeRequirements
from evalspleef.spi.errors import SpleefError

from .capacity import CapacitySpec, InstanceCapacity, Reservation, ResourceRequest

__all__ = [
    "QUEUE_ALERT_S",
    "STALE_HEARTBEAT_S",
    "STARVATION_S",
    "CapacityUnavailable",
    "HostLoss",
    "Priority",
    "Scheduler",
    "SchedulerAlert",
    "Ticket",
    "WorkItem",
    "priority_for",
]

#: Queue wait that raises an alert (PRD appendix B.2a, step S1).
QUEUE_ALERT_S = 30 * 60.0
#: A waiter without resources for this long is escalated (docs/02 section 9.1, principle 5).
STARVATION_S = 2 * 60 * 60.0
#: A host without heartbeat for longer than this is drained (docs/02 section 9.1).
STALE_HEARTBEAT_S = 60.0


class Priority(IntEnum):
    """Lower value is served first (docs/02 section 9.1, principle 3)."""

    NICHE_PLATFORM = 0
    RERUN = 1
    REGULAR = 2
    CONTROL = 3


def priority_for(
    requirements: RuntimeRequirements, *, rerun: bool = False, control: bool = False
) -> Priority:
    """Priority of a trial: non-Linux or GPU platforms are the scarce resource and go first."""
    if requirements.os != OSFamily.LINUX or requirements.gpu:
        return Priority.NICHE_PLATFORM
    if rerun:
        return Priority.RERUN
    if control:
        return Priority.CONTROL
    return Priority.REGULAR


class CapacityUnavailable(SpleefError):
    """No route instance can ever host the item (for example every host was lost)."""

    error_class = "environment"
    disposition = "discard"


@dataclass(frozen=True)
class WorkItem:
    """One trial attempt that needs a sandbox slot."""

    trial_id: str
    cell_id: str
    model: str
    routes: tuple[str, ...]
    """Candidate instances from static matching, in route order."""
    request: ResourceRequest
    priority: Priority = Priority.REGULAR


@dataclass(frozen=True)
class SchedulerAlert:
    kind: Literal["queue_timeout", "starvation"]
    trial_id: str
    waited_s: float


@dataclass(frozen=True)
class HostLoss:
    instance: str
    host_id: str
    trial_ids: tuple[str, ...]
    """Trials whose reservation was on the lost host and must be retried."""


class Ticket:
    """A queued request. ``wait`` resolves to the granted :class:`Reservation`."""

    def __init__(self, item: WorkItem, seq: int, enqueued_at: float) -> None:
        self.item = item
        self.seq = seq
        self.enqueued_at = enqueued_at
        self.future: asyncio.Future[Reservation] = asyncio.get_running_loop().create_future()
        self.alerted = False
        self.escalated = False

    async def wait(self) -> Reservation:
        """Raises :class:`CapacityUnavailable` if the item can no longer be placed anywhere."""
        return await self.future


class Scheduler:
    """Capacity semaphores per instance plus one fair, prioritised waiting queue."""

    def __init__(
        self,
        capacities: Iterable[CapacitySpec],
        *,
        clock: Callable[[], float] = time.monotonic,
        on_alert: Callable[[SchedulerAlert], None] | None = None,
        queue_alert_s: float = QUEUE_ALERT_S,
        starvation_s: float = STARVATION_S,
        heartbeat_timeout_s: float = STALE_HEARTBEAT_S,
    ) -> None:
        self._capacities = {spec.instance: InstanceCapacity(spec) for spec in capacities}
        self._clock = clock
        self._on_alert = on_alert
        self._queue_alert_s = queue_alert_s
        self._starvation_s = starvation_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self.alerts: list[SchedulerAlert] = []
        """Every alert raised so far, in order."""
        self._waiters: list[Ticket] = []
        self._served: Counter[str] = Counter()
        self._seq = 0
        now = clock()
        self._heartbeats = {
            (instance, host_id): now
            for instance, capacity in self._capacities.items()
            for host_id in capacity.available_hosts
        }

    # -- queueing ------------------------------------------------------------------------

    def capacity(self, instance: str) -> InstanceCapacity:
        return self._capacities[instance]

    @property
    def pending(self) -> int:
        """Number of items still waiting for a slot."""
        return sum(1 for ticket in self._waiters if not ticket.future.done())

    def enqueue(self, item: WorkItem) -> Ticket:
        """Queue an item without dispatching, so callers can enqueue a whole batch first and
        let one :meth:`dispatch` pass apply fairness across all of it."""
        self._seq += 1
        ticket = Ticket(item, self._seq, self._clock())
        self._waiters.append(ticket)
        return ticket

    async def acquire(self, item: WorkItem) -> Reservation:
        """Enqueue, dispatch and wait; a cancelled caller never leaks its slot."""
        ticket = self.enqueue(item)
        self.dispatch()
        try:
            return await ticket.wait()
        except asyncio.CancelledError:
            self.cancel(ticket)
            raise

    def cancel(self, ticket: Ticket) -> None:
        """Withdraw a ticket; a slot that was already granted is returned."""
        if ticket in self._waiters:
            self._waiters.remove(ticket)
        future = ticket.future
        if not future.done():
            future.cancel()
        elif not future.cancelled() and future.exception() is None:
            self.release(future.result())

    def release(self, reservation: Reservation) -> None:
        """Return a slot and hand it to the best waiter."""
        self._capacities[reservation.instance].release(reservation)
        self.dispatch()

    def dispatch(self) -> int:
        """Grant every slot that is free; returns the number of grants."""
        self.check_waiting()
        granted = 0
        while True:
            outcome = self._step()
            if outcome is None:
                return granted
            granted += int(outcome)

    def _order_key(self, ticket: Ticket) -> tuple[int, int, int]:
        rank = -1 if ticket.escalated else int(ticket.item.priority)
        return (rank, self._served[ticket.item.model], ticket.seq)

    def _try_place(self, item: WorkItem) -> Reservation | None:
        for instance in item.routes:
            capacity = self._capacities.get(instance)
            if capacity is None:
                continue
            reservation = capacity.try_acquire(item.trial_id, item.request)
            if reservation is not None:
                return reservation
        return None

    def _placeable(self, item: WorkItem) -> bool:
        return any(
            instance in self._capacities and self._capacities[instance].can_ever_fit(item.request)
            for instance in item.routes
        )

    def _step(self) -> bool | None:
        """One scheduling decision: ``True`` granted, ``False`` failed an item, ``None`` idle."""
        self._waiters = [ticket for ticket in self._waiters if not ticket.future.done()]
        for ticket in sorted(self._waiters, key=self._order_key):
            reservation = self._try_place(ticket.item)
            if reservation is not None:
                self._waiters.remove(ticket)
                self._served[ticket.item.model] += 1
                ticket.future.set_result(reservation)
                return True
            if not self._placeable(ticket.item):
                self._waiters.remove(ticket)
                ticket.future.set_exception(
                    CapacityUnavailable(
                        f"no capacity can host {ticket.item.trial_id} on {ticket.item.routes}"
                    )
                )
                return False
        return None

    # -- alerts --------------------------------------------------------------------------

    def check_waiting(self) -> list[SchedulerAlert]:
        """Raise queue-timeout and starvation alerts once per waiter."""
        now = self._clock()
        alerts: list[SchedulerAlert] = []
        for ticket in self._waiters:
            if ticket.future.done():
                continue
            waited = now - ticket.enqueued_at
            if waited >= self._queue_alert_s and not ticket.alerted:
                ticket.alerted = True
                alerts.append(SchedulerAlert("queue_timeout", ticket.item.trial_id, waited))
            if waited >= self._starvation_s and not ticket.escalated:
                ticket.escalated = True
                alerts.append(SchedulerAlert("starvation", ticket.item.trial_id, waited))
        self.alerts.extend(alerts)
        if self._on_alert is not None:
            for alert in alerts:
                self._on_alert(alert)
        return alerts

    # -- host loss -----------------------------------------------------------------------

    def heartbeat(self, instance: str, host_id: str) -> None:
        """Record that a host is alive."""
        if (instance, host_id) in self._heartbeats:
            self._heartbeats[(instance, host_id)] = self._clock()

    def host_lost(self, instance: str, host_id: str) -> HostLoss:
        """Remove a host: its slots disappear, its running trials get ``lost`` set."""
        affected = self._capacities[instance].remove_host(host_id)
        self._heartbeats.pop((instance, host_id), None)
        self.dispatch()
        return HostLoss(instance, host_id, tuple(r.trial_id for r in affected))

    def sweep_stale_hosts(self) -> list[HostLoss]:
        """Drain every host whose last heartbeat is older than the timeout."""
        now = self._clock()
        stale = [
            key for key, seen in self._heartbeats.items() if now - seen > self._heartbeat_timeout_s
        ]
        return [self.host_lost(instance, host_id) for instance, host_id in stale]
