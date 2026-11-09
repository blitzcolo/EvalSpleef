"""Lease manager: create, destroy and reap sandboxes (PRD FR-SBX-02, TECH-DESIGN sections 10, 11).

Every sandbox a trial uses is a lease. A lease is created with the labels ``batch``, ``trial``,
``lease`` and ``provider`` and a TTL, recorded as a ``sandbox_lease`` row, and announced with
``trial.leased`` (agent role only) and ``sandbox.provisioned``. :meth:`LeaseManager.lease` always
destroys the sandbox in a ``finally`` path and records ``teardown.started`` and
``teardown.completed``. A reap loop asks every provider instance to destroy orphans of the batch
every ten minutes and records ``sandbox.orphan.reaped``.

Invariant I10: a verifier runs in its own sandbox with ``network=off``, never in the agent's.

Right after ``create`` every sandbox is measured through the Sandbox SPI (kernel, OS release,
CPUs, memory as the guest sees them, :func:`~evalspleef.control.environment.measure_guest`);
the result travels in ``sandbox.provisioned`` and on the lease, and a guest that cannot be
measured is recorded as ``unmeasured`` with the reason instead of failing the lease.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.spi.caps import NetMode, RuntimeRequirements
from evalspleef.spi.errors import CapabilityNotSupported, PolicyNotEnforceable, SpleefError
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxProvider,
    SandboxSpec,
    TemplateRef,
)

from ..environment import measure_guest
from ..events import EventStore
from ..store.repos import SandboxLeaseRepository
from ..store.rows import SandboxLeaseRow

__all__ = [
    "REAP_INTERVAL_S",
    "LeaseInvariantError",
    "LeaseManager",
    "LeaseRequest",
    "LeaseRole",
    "LeasedSandbox",
    "OrphanSandbox",
    "SandboxProviders",
]

#: Orphan scan period (TECH-DESIGN section 10, "cleanup").
REAP_INTERVAL_S = 600.0

LeaseRole = Literal["agent", "verifier"]

_ACTOR = "lease-manager"


class LeaseInvariantError(SpleefError):
    """A lease would break invariant I10 (verifier isolation)."""

    error_class = "isolation"
    disposition = "discard"


class SandboxProviders(Protocol):
    """The slice of ``ProviderRegistry`` the lease manager needs."""

    def sandbox_instance(self, name: str) -> SandboxProvider: ...

    def sandbox_instances(self) -> Mapping[str, SandboxProvider]: ...


@dataclass(frozen=True)
class LeaseRequest:
    batch_id: str
    trial_id: str
    role: LeaseRole
    instance: str
    requirements: RuntimeRequirements
    template: TemplateRef
    host_id: str | None = None
    """Host the scheduler placed the trial on, recorded in the lease row."""
    network: NetworkPolicy | None = None
    """Defaults to the task's network mode. Verifiers are always ``off``."""
    env: Mapping[str, str] = field(default_factory=dict)
    run_as_user: str = "agent"
    ttl_s: int | None = None
    """Defaults to the task's ``max_lifetime_s``."""


@dataclass(eq=False)
class LeasedSandbox:
    """A live lease. ``destroyed`` flips only when the provider confirmed the destroy."""

    lease_id: str
    batch_id: str
    trial_id: str
    role: LeaseRole
    instance: str
    host_id: str | None
    sandbox: Sandbox
    ttl_s: int
    created_at: str
    destroyed: bool = False
    teardown_error: str | None = None
    guest: dict[str, Any] | None = None
    """The guest measured right after create (``measure_guest``); ``None`` until measured."""


@dataclass(frozen=True)
class OrphanSandbox:
    """A sandbox the provider lists for the batch that no live lease accounts for."""

    instance: str
    sandbox_id: str | None
    lease_id: str | None
    trial_id: str | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_label(entry: Mapping[str, Any], key: str) -> str | None:
    labels = entry.get("labels")
    if isinstance(labels, Mapping):
        value = labels.get(key)
        return str(value) if value is not None else None
    return None


def _entry_id(entry: Mapping[str, Any]) -> str | None:
    value = entry.get("id")
    return str(value) if value is not None else None


class LeaseManager:
    """Creates and destroys the sandboxes of a batch and records every step."""

    def __init__(
        self,
        providers: SandboxProviders,
        leases: SandboxLeaseRepository,
        events: EventStore,
        *,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        new_id: Callable[[], str] | None = None,
        reap_interval_s: float = REAP_INTERVAL_S,
        default_ttl_s: int = 3600,
        orphan_grace_s: int = 600,
        templates: Callable[[TemplateRef, str], TemplateRef] | None = None,
    ) -> None:
        """``templates`` maps a plan's OCI template to the one a provider instance boots
        (TECH-DESIGN 7.6); without it every instance gets the plan's template."""
        self._providers = providers
        self._templates = templates
        self._leases = leases
        self._events = events
        self._now = now
        self._sleep = sleep
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:12])
        self._reap_interval_s = reap_interval_s
        self._max_ttl_s = default_ttl_s
        self._orphan_grace_s = orphan_grace_s
        self._active: dict[str, LeasedSandbox] = {}

    # -- create ------------------------------------------------------------------------

    @property
    def active(self) -> tuple[LeasedSandbox, ...]:
        """Leases that were created and not yet confirmed destroyed."""
        return tuple(self._active.values())

    async def provision(self, request: LeaseRequest) -> LeasedSandbox:
        """Create a sandbox, record the lease row and the leasing events.

        The caller owns the result and must call :meth:`teardown`; prefer :meth:`lease`.
        """
        provider = self._providers.sandbox_instance(request.instance)
        lease_id = f"lse_{self._new_id()}"
        try:
            spec = self._build_spec(request, lease_id, provider)
            sandbox = await provider.create(spec)
        except (CapabilityNotSupported, PolicyNotEnforceable) as exc:
            await self._emit(
                EventType.PROVIDER_CAPABILITY_REJECT,
                request.batch_id,
                request.trial_id,
                {
                    "provider": request.instance,
                    "role": request.role,
                    "error": type(exc).__name__,
                    "message": exc.message,
                },
            )
            raise
        leased = LeasedSandbox(
            lease_id=lease_id,
            batch_id=request.batch_id,
            trial_id=request.trial_id,
            role=request.role,
            instance=request.instance,
            host_id=request.host_id,
            sandbox=sandbox,
            ttl_s=spec.max_lifetime_s,
            created_at=_iso(self._now()),
        )
        self._assert_verifier_isolated(leased)
        self._max_ttl_s = max(self._max_ttl_s, leased.ttl_s)
        try:
            self._active[lease_id] = leased
            leased.guest = await measure_guest(sandbox, spec.os)
            await self._leases.insert(
                SandboxLeaseRow(
                    lease_id=lease_id,
                    batch_id=request.batch_id,
                    trial_id=request.trial_id,
                    role=request.role,
                    template_kind=spec.template.kind,
                    template_ref=spec.template.ref,
                    template_digest=spec.template.digest,
                    created_at=leased.created_at,
                    provider_instance=request.instance,
                    host_id=request.host_id,
                )
            )
            payload: dict[str, Any] = {
                "lease_id": lease_id,
                "role": request.role,
                "provider": request.instance,
                "host_id": request.host_id,
                "sandbox_id": sandbox.id,
                "template_digest": request.template.digest,
                "ttl_s": leased.ttl_s,
                "guest": leased.guest,
            }
            if request.role == "agent":
                # The agent lease is the trial's pending -> leased transition.
                transition = {"event": "lease", "from": "pending", "to": "leased"}
                await self._emit(
                    EventType.TRIAL_LEASED,
                    request.batch_id,
                    request.trial_id,
                    {"transition": transition, **payload},
                )
            await self._emit(
                EventType.SANDBOX_PROVISIONED, request.batch_id, request.trial_id, payload
            )
        except BaseException:
            # The sandbox exists but the lease could not be recorded: never leave it running.
            await self.teardown(leased)
            raise
        return leased

    @asynccontextmanager
    async def lease(self, request: LeaseRequest) -> AsyncIterator[LeasedSandbox]:
        """Provision a sandbox and destroy it on exit, whatever happens in the body."""
        leased = await self.provision(request)
        try:
            yield leased
        finally:
            await self.teardown(leased)

    def _build_spec(
        self, request: LeaseRequest, lease_id: str, provider: SandboxProvider
    ) -> SandboxSpec:
        requirements = request.requirements
        arches = sorted(requirements.arch & provider.capabilities().arches)
        if not arches:
            raise CapabilityNotSupported(
                f"{request.instance} supports none of the required architectures"
            )
        if request.role == "verifier":
            if request.network is not None and request.network.mode != NetMode.OFF:
                raise LeaseInvariantError(
                    f"verifier sandboxes run with network=off, requested {request.network.mode}"
                )
            network = NetworkPolicy(mode=NetMode.OFF)
        else:
            network = request.network or NetworkPolicy(
                mode=requirements.network, allow_hosts=requirements.allow_hosts
            )
        return SandboxSpec(
            trial_id=request.trial_id,
            lease_id=lease_id,
            role=request.role,
            os=requirements.os,
            arch=arches[0],
            template=(
                self._templates(request.template, request.instance)
                if self._templates is not None
                else request.template
            ),
            resources=Resources(
                cpus=requirements.cpus,
                memory_mb=requirements.memory_mb,
                disk_mb=requirements.disk_mb,
                gpus=1 if requirements.gpu else 0,
            ),
            network=network,
            run_as_user=request.run_as_user,
            env=dict(request.env),
            labels={
                "batch": request.batch_id,
                "trial": request.trial_id,
                "lease": lease_id,
                "provider": request.instance,
            },
            max_lifetime_s=request.ttl_s or requirements.max_lifetime_s,
            gui=requirements.gui,
        )

    def _assert_verifier_isolated(self, leased: LeasedSandbox) -> None:
        """I10: the verifier is a different lease on a different sandbox than any live agent."""
        if leased.role != "verifier":
            return
        for other in self._active.values():
            if (
                other.role == "agent"
                and other.trial_id == leased.trial_id
                and (
                    other.lease_id == leased.lease_id
                    or (other.instance == leased.instance and other.sandbox.id == leased.sandbox.id)
                )
            ):
                raise LeaseInvariantError(
                    f"I10: verifier of {leased.trial_id} would share sandbox "
                    f"{leased.sandbox.id} with the agent lease {other.lease_id}"
                )

    # -- destroy -----------------------------------------------------------------------

    async def teardown(self, leased: LeasedSandbox) -> None:
        """Destroy the sandbox and record the outcome; safe to call repeatedly.

        A failing destroy is recorded in ``teardown.completed`` (``ok=false``) and leaves the
        lease row open, so the reaper finds and destroys the sandbox later.
        """
        if leased.destroyed:
            return
        await self._emit(
            EventType.TEARDOWN_STARTED,
            leased.batch_id,
            leased.trial_id,
            {"lease_id": leased.lease_id, "role": leased.role, "provider": leased.instance},
        )
        error: str | None = None
        try:
            await leased.sandbox.destroy()
        except Exception as exc:  # recorded in teardown.completed below, finished by the reaper
            error = f"{type(exc).__name__}: {exc}"
        if error is None:
            leased.destroyed = True
            leased.teardown_error = None
            self._active.pop(leased.lease_id, None)
            await self._leases.mark_destroyed(leased.lease_id, _iso(self._now()))
        else:
            leased.teardown_error = error
        await self._emit(
            EventType.TEARDOWN_COMPLETED,
            leased.batch_id,
            leased.trial_id,
            {
                "lease_id": leased.lease_id,
                "role": leased.role,
                "provider": leased.instance,
                "ok": error is None,
                "error": error,
            },
        )

    # -- reap --------------------------------------------------------------------------

    def _orphan_after_s(self) -> int:
        """A live trial never outlives its TTL, so only sandboxes older than that are orphans."""
        return self._max_ttl_s + self._orphan_grace_s

    async def find_orphans(self, batch_id: str) -> list[OrphanSandbox]:
        """Sandboxes listed for the batch that no live lease accounts for."""
        orphans: list[OrphanSandbox] = []
        for instance, provider in self._providers.sandbox_instances().items():
            for entry in await provider.list({"batch": batch_id}):
                lease_id = _entry_label(entry, "lease")
                if lease_id is not None and lease_id in self._active:
                    continue
                orphans.append(
                    OrphanSandbox(
                        instance, _entry_id(entry), lease_id, _entry_label(entry, "trial")
                    )
                )
        return orphans

    async def reap_once(self, batch_id: str) -> int:
        """Ask every provider instance to destroy the batch's orphans; returns how many."""
        labels = {"batch": batch_id}
        total = 0
        for instance, provider in self._providers.sandbox_instances().items():
            before = await provider.list(labels)
            count = await provider.reap(labels, self._orphan_after_s())
            if count <= 0:
                continue
            total += count
            remaining = {_entry_id(entry) for entry in await provider.list(labels)}
            gone = [entry for entry in before if _entry_id(entry) not in remaining]
            for entry in gone:
                lease_id = _entry_label(entry, "lease")
                if lease_id is not None:
                    await self._leases.mark_destroyed(lease_id, _iso(self._now()), reaped=True)
                    live = self._active.pop(lease_id, None)
                    if live is not None:
                        live.destroyed = True
                await self._emit(
                    EventType.SANDBOX_ORPHAN_REAPED,
                    batch_id,
                    _entry_label(entry, "trial"),
                    {
                        "provider": instance,
                        "lease_id": lease_id,
                        "sandbox_id": _entry_id(entry),
                        "count": 1,
                    },
                )
            unattributed = count - len(gone)
            if unattributed > 0:
                await self._emit(
                    EventType.SANDBOX_ORPHAN_REAPED,
                    batch_id,
                    None,
                    {
                        "provider": instance,
                        "lease_id": None,
                        "sandbox_id": None,
                        "count": unattributed,
                    },
                )
        return total

    async def run_reaper(self, batch_id: str, stop: asyncio.Event) -> None:
        """Reap every ``reap_interval_s`` until ``stop`` is set."""
        while not stop.is_set():
            sleeper = asyncio.ensure_future(self._sleep(self._reap_interval_s))
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (sleeper, stopper):
                    waiter.cancel()
                await asyncio.gather(sleeper, stopper, return_exceptions=True)
            if stop.is_set():
                return
            await self.reap_once(batch_id)

    # -- events ------------------------------------------------------------------------

    async def _emit(
        self, event_type: EventType, batch_id: str, trial_id: str | None, payload: Mapping[str, Any]
    ) -> None:
        await self._events.append(
            EventEnvelope(
                event_id=new_event_id(),
                ts=_iso(self._now()),
                batch_id=batch_id,
                type=event_type,
                actor=_ACTOR,
                trial_id=trial_id,
                payload=dict(payload),
            )
        )
