"""EndpointManager: lifecycle state machine, operator tickets, warm-up and retirement.

The event log is authoritative (AGENTS.md rule 6): every transition appends an ``endpoint.*``
event first and then updates the ``endpoint`` row. The manager keeps no state of its own; each
operation reloads the endpoint from the stores, so provider calls that mutate a handle in place
can never corrupt it when they raise.

Probes need a live gateway route, so ``probe`` registers the route before the suite runs and
removes it again when a required probe fails. The price is set only after the probe passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from evalspleef.control.events.base import EventStore
from evalspleef.control.hf.mirror import MIRROR_DEFAULT
from evalspleef.control.hf.tokens import check_token_expiry
from evalspleef.control.probes.client import as_dict
from evalspleef.control.probes.clock import Clock, SystemClock, iso_utc
from evalspleef.control.probes.config import ProbeTarget
from evalspleef.control.probes.report import report_from_row, report_to_row
from evalspleef.control.probes.runner import PingResult, ProbeRunner
from evalspleef.control.providers.registry import ProviderRegistry
from evalspleef.control.registry.model_registry import ModelRegistry
from evalspleef.control.store.repos import (
    EndpointRepository,
    EndpointTicketRepository,
    ProbeReportRepository,
)
from evalspleef.control.store.rows import EndpointRow, EndpointTicketRow
from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.gateway import GatewayAdmin, PriceSpec
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DEPLOYMENT_ID_KEY,
    EndpointHandle,
    EndpointState,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelRole,
    ModelSpec,
    TeardownMode,
    WireProtocol,
)
from evalspleef.spi.probe import ProbeReport

from .states import (
    TRANSITIONS,
    EndpointNotFound,
    InvalidTransition,
    TicketError,
    event_type_for,
    make_endpoint_id,
    public_name,
)
from .tickets import TICKET_CLOSED, TICKET_EXPIRED, TICKET_OPEN, ticket_expires_at, ticket_text

__all__ = [
    "EndpointManager",
    "EndpointManagerConfig",
    "GuardReport",
    "IdleSpinAlert",
    "WarmResult",
    "probe_failures",
    "required_replicas",
]

S = EndpointState

#: Probes every endpoint must pass: chat + usage, streaming, identity (PRD FR-MOD-05).
REQUIRED_PROBES = ("P-01", "P-02", "P-14")
#: Judge and simulated-user roles additionally need structured output.
STRUCTURED_ROLES: frozenset[ModelRole] = frozenset({"judge", "user"})

_PROBEABLE = frozenset({S.VERIFYING, S.PROBE_FAILED, S.READY, S.WARM, S.DEGRADED})
_WARMABLE = frozenset({S.READY, S.WARM, S.SLEEPING, S.DEGRADED})
_HEALTH_CHECKED = frozenset({S.READY, S.WARM, S.SLEEPING, S.DEGRADED})
_USABLE = frozenset({S.READY, S.WARM})


@dataclass(frozen=True)
class EndpointManagerConfig:
    event_batch_id: str = "endpoints"
    """Event stream for events that belong to no batch (registration, probes, tickets)."""
    actor: str = "endpoint-manager"
    ticket_ttl_s: float = 86_400.0
    wake_timeout_s: int = 600
    warm_ping_timeout_s: float = 600.0
    cold_start_threshold_s: float = 60.0
    """A first request slower than this counts as a cold start (PRD FR-MOD-03)."""
    idle_threshold_s: float = 900.0
    """A WARM endpoint without traffic for this long raises the idle-spin alert."""


@dataclass(frozen=True)
class WarmResult:
    endpoint_id: str
    state: EndpointState
    replicas_required: int
    replicas_confirmed: bool
    """False while an assisted scale ticket is still open."""
    cold_start_s: float | None = None
    ticket_id: str | None = None


@dataclass(frozen=True)
class GuardReport:
    ok: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdleSpinAlert:
    endpoint_id: str
    idle_s: float


IdleAlertHook = Callable[[IdleSpinAlert], Awaitable[None]]
PriceFor = Callable[[ModelSpec], PriceSpec]


def required_replicas(
    *, peak_concurrent_trials: int, requests_per_trial: int, max_concurrency_per_replica: int
) -> int:
    """``ceil(peak concurrent trials x requests per trial / max concurrency per replica)``."""
    if max_concurrency_per_replica <= 0:
        raise ValueError("max_concurrency_per_replica must be positive")
    if peak_concurrent_trials < 0 or requests_per_trial < 0:
        raise ValueError("trial and request counts must not be negative")
    return math.ceil(peak_concurrent_trials * requests_per_trial / max_concurrency_per_replica)


def probe_failures(report: ProbeReport, roles: Iterable[ModelRole]) -> list[str]:
    """Ids of the required probes that did not pass; an empty list means the endpoint is READY."""
    required = list(REQUIRED_PROBES)
    if STRUCTURED_ROLES & set(roles):
        required.append("P-07")
    passed = {check.probe_id for check in report.checks if check.passed}
    return [probe_id for probe_id in required if probe_id not in passed]


def _protocol(value: object) -> WireProtocol:
    if value == "responses":
        return "responses"
    if value == "anthropic_messages":
        return "anthropic_messages"
    return "chat_completions"


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class EndpointManager:
    def __init__(
        self,
        *,
        models: ModelRegistry,
        providers: ProviderRegistry,
        endpoints: EndpointRepository,
        tickets: EndpointTicketRepository,
        reports: ProbeReportRepository,
        events: EventStore,
        gateway: GatewayAdmin,
        probes: ProbeRunner,
        price_for: PriceFor,
        hf: HFAccessChecker | None = None,
        clock: Clock | None = None,
        config: EndpointManagerConfig | None = None,
        hf_token_expiry: Mapping[str, date | None] | None = None,
        idle_alert: IdleAlertHook | None = None,
    ) -> None:
        self._models = models
        self._providers = providers
        self._endpoints = endpoints
        self._tickets = tickets
        self._reports = reports
        self._events = events
        self._gateway = gateway
        self._probes = probes
        self._price_for = price_for
        self._hf = hf
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._cfg = config if config is not None else EndpointManagerConfig()
        self._hf_token_expiry: Mapping[str, date | None] = hf_token_expiry or {}
        self._idle_alert = idle_alert

    # -- persistence and events -----------------------------------------------------

    async def get(self, endpoint_id: str) -> EndpointHandle:
        """Reload an endpoint from the stores."""
        row = await self._endpoints.get(endpoint_id)
        if row is None:
            raise EndpointNotFound(f"no endpoint {endpoint_id!r}")
        spec = await self._models.get(row.model_slug, row.model_spec_version)
        if spec is None:
            raise EndpointNotFound(
                f"endpoint {endpoint_id!r} references unregistered model "
                f"{row.model_slug}@{row.model_spec_version}"
            )
        observed = dict(row.observed)
        return EndpointHandle(
            endpoint_id=row.endpoint_id,
            model=spec,
            state=EndpointState(row.state),
            base_url=_opt_str(observed.pop("base_url", None)),
            upstream_model=_opt_str(observed.pop("upstream_model", None)),
            protocol=_protocol(observed.pop("protocol", None)),
            credential_ref=str(observed.pop("credential_ref", "")),
            external_ids=dict(row.external_ids),
            observed=observed,
        )

    async def latest_report(self, endpoint_id: str) -> ProbeReport | None:
        handle = await self.get(endpoint_id)
        report_id = _opt_str(handle.observed.get("report_id"))
        if report_id is None:
            return None
        row = await self._reports.get(report_id)
        return report_from_row(row) if row is not None else None

    async def _save(self, handle: EndpointHandle) -> None:
        existing = await self._endpoints.get(handle.endpoint_id)
        now = iso_utc(self._clock.now())
        observed = dict(handle.observed)
        observed.update(
            base_url=handle.base_url,
            upstream_model=handle.upstream_model,
            protocol=handle.protocol,
            credential_ref=handle.credential_ref,
        )
        retired_at = existing.retired_at if existing is not None else None
        if handle.state is S.RETIRED and retired_at is None:
            retired_at = now
        await self._endpoints.upsert(
            EndpointRow(
                endpoint_id=handle.endpoint_id,
                model_slug=handle.model.slug,
                model_spec_version=handle.model.spec_version,
                adapter=handle.model.deployment.adapter,
                account=handle.model.deployment.account,
                state=handle.state.value,
                external_ids=dict(handle.external_ids),
                observed=observed,
                created_at=existing.created_at if existing is not None else now,
                retired_at=retired_at,
            )
        )

    async def _emit(
        self,
        event_type: EventType,
        handle: EndpointHandle,
        *,
        from_state: EndpointState | None,
        reason: str,
        batch_id: str | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "endpoint_id": handle.endpoint_id,
            "from_state": from_state.value if from_state is not None else None,
            "state": handle.state.value,
            "reason": reason,
            **extra,
        }
        await self._events.append(
            EventEnvelope(
                event_id=new_event_id(clock=self._clock.now),
                ts=iso_utc(self._clock.now()),
                batch_id=batch_id or self._cfg.event_batch_id,
                type=event_type,
                actor=self._cfg.actor,
                payload=payload,
            )
        )

    async def _transition(
        self,
        handle: EndpointHandle,
        new_state: EndpointState,
        *,
        reason: str,
        batch_id: str | None = None,
        event_type: EventType | None = None,
        **extra: Any,
    ) -> None:
        old = handle.state
        if new_state not in TRANSITIONS[old]:
            raise InvalidTransition(
                f"endpoint {handle.endpoint_id}: {old.value} -> {new_state.value} is not allowed"
            )
        handle.state = new_state
        if new_state is S.WARM:
            handle.observed["warm_since"] = self._clock.now()
            handle.observed.pop("idle_alerted", None)
        await self._emit(
            event_type or event_type_for(new_state),
            handle,
            from_state=old,
            reason=reason,
            batch_id=batch_id,
            **extra,
        )
        await self._save(handle)

    def _provider(self, handle: EndpointHandle) -> ModelEndpointProvider:
        return self._providers.model_provider(handle.model.deployment.account)

    # -- registration -----------------------------------------------------------------

    async def _check_source(self, spec: ModelSpec, *, freeze: bool) -> tuple[ModelSpec, list[str]]:
        """HF resolve + preflight; with ``freeze`` also file hashes and the private mirror."""
        source = spec.source
        if not isinstance(source, HFSource):
            return spec, []
        if self._hf is None:
            return spec, ["no HF access checker is configured for a Hugging Face source"]
        source = dataclasses.replace(
            source,
            revision=await self._hf.resolve_revision(
                source.repo, source.revision, source.token_ref
            ),
        )
        report = await self._hf.preflight(source)
        if not report.ok:
            failed = [f"{cid}: {detail}" for cid, passed, detail in report.checks if not passed]
            return spec, failed or ["Hugging Face preflight failed"]
        if freeze:
            if not source.files_sha256:
                source = dataclasses.replace(
                    source, files_sha256=dict(await self._hf.small_file_hashes(source))
                )
            if spec.deployment.params.get("mirror", MIRROR_DEFAULT) and source.mirror_repo is None:
                source = dataclasses.replace(
                    source, mirror_repo=await self._hf.ensure_mirror(source)
                )
        return dataclasses.replace(spec, source=source), []

    async def register(self, spec: ModelSpec) -> EndpointHandle:
        """validate -> HF resolve/preflight/mirror -> plan -> deploy (TECH-DESIGN 6.2).

        Registering the same ``slug@spec_version`` again returns the existing endpoint unless it
        was REJECTED, in which case the checks run again (the cause may have been fixed).
        """
        endpoint_id = make_endpoint_id(spec)
        existing = await self._endpoints.get(endpoint_id)
        if existing is not None and existing.state != S.REJECTED.value:
            return await self.get(endpoint_id)
        provider = self._providers.model_provider(spec.deployment.account)

        handle = EndpointHandle(
            endpoint_id=endpoint_id,
            model=spec,
            state=S.REGISTERED,
            base_url=None,
            upstream_model=None,
            protocol="chat_completions",
            credential_ref="",
        )
        if existing is None:
            await self._emit(
                EventType.ENDPOINT_REGISTERED,
                handle,
                from_state=None,
                reason="registered",
                slug=spec.slug,
                spec_version=spec.spec_version,
                adapter=spec.deployment.adapter,
            )
            await self._save(handle)
        else:
            handle.state = S.REJECTED
            await self._transition(handle, S.REGISTERED, reason="re-registered")

        stored = await self._models.get(spec.slug, spec.spec_version)
        try:
            frozen, failures = await self._check_source(
                stored if stored is not None else spec, freeze=stored is None
            )
            if not failures:
                report = await provider.validate(frozen)
                if not report.ok:
                    failures = [
                        f"{cid}: {detail}" for cid, passed, detail in report.checks if not passed
                    ] or ["provider validation failed"]
        except SpleefError as exc:
            frozen, failures = spec, [f"{type(exc).__name__}: {exc.message}"]
        if failures:
            await self._transition(handle, S.REJECTED, reason="rejected", failures=failures)
            return handle
        if stored is None:
            await self._models.register(frozen)

        plan = await provider.plan(frozen)
        deployed = await provider.deploy(frozen, plan)
        deployed.endpoint_id = endpoint_id
        deployed.state, provider_state = S.REGISTERED, deployed.state
        deployed.observed["plan"] = dataclasses.asdict(plan)
        target = {
            S.AWAITING_OPERATOR: S.AWAITING_OPERATOR,
            S.DEPLOYING: S.DEPLOYING,
            S.FAILED: S.FAILED,
        }.get(provider_state, S.VERIFYING)
        if target is S.AWAITING_OPERATOR:
            ticket = await self._open_ticket(
                endpoint_id,
                "deploy",
                plan.operator_instructions_md
                or f"Deploy {frozen.slug} on account {frozen.deployment.account}, then attach "
                "base_url, model and deployment id.",
            )
            deployed.observed["ticket_id"] = ticket.ticket_id
        await self._transition(deployed, target, reason=f"deploy:{provider.management_mode.value}")
        return deployed

    async def refresh(self, endpoint_id: str) -> EndpointHandle:
        """Poll an API-mode deployment (DEPLOYING) and advance it once the provider is ready."""
        handle = await self.get(endpoint_id)
        if handle.state is not S.DEPLOYING:
            return handle
        current = handle.state
        status = await self._provider(handle).status(handle)
        handle.state = current
        if status is S.FAILED:
            await self._transition(handle, S.FAILED, reason="deploy_failed")
        elif status in (S.VERIFYING, S.READY, S.WARM):
            await self._transition(handle, S.VERIFYING, reason="deployed")
        return handle

    # -- tickets ------------------------------------------------------------------------

    async def _open_ticket(
        self, endpoint_id: str, kind: str, instructions_md: str
    ) -> EndpointTicketRow:
        ticket = EndpointTicketRow(
            ticket_id=f"tkt_{uuid.uuid4().hex[:12]}",
            endpoint_id=endpoint_id,
            kind=kind,
            instructions_md=instructions_md,
            status=TICKET_OPEN,
            opened_at=iso_utc(self._clock.now()),
        )
        await self._tickets.insert(ticket)
        return ticket

    async def _close_ticket(self, ticket_id: str, status: str, operator: str | None = None) -> None:
        await self._tickets.update_status(
            ticket_id, status, operator=operator, closed_at=iso_utc(self._clock.now())
        )

    async def tickets(self, endpoint_id: str) -> list[EndpointTicketRow]:
        return await self._tickets.list_by_endpoint(endpoint_id)

    def render_ticket(self, ticket: EndpointTicketRow) -> str:
        """CLI-printable instruction text for ``ticket``."""
        return ticket_text(ticket, self._cfg.ticket_ttl_s)

    async def expire_tickets(self) -> list[str]:
        """Expire deploy tickets older than the TTL; their endpoints become FAILED."""
        now = self._clock.now()
        expired: list[str] = []
        for row in await self._endpoints.list_by_state(S.AWAITING_OPERATOR.value):
            for ticket in await self._tickets.list_by_endpoint(row.endpoint_id):
                is_due = now >= ticket_expires_at(ticket, self._cfg.ticket_ttl_s)
                if ticket.kind == "deploy" and ticket.status == TICKET_OPEN and is_due:
                    await self._close_ticket(ticket.ticket_id, TICKET_EXPIRED)
                    handle = await self.get(row.endpoint_id)
                    await self._transition(
                        handle, S.FAILED, reason="ticket_expired", ticket_id=ticket.ticket_id
                    )
                    expired.append(row.endpoint_id)
        return expired

    async def confirm_ticket(self, ticket_id: str, operator: str) -> EndpointHandle:
        """Operator reports a scale or teardown ticket as done (deploy tickets use ``attach``)."""
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketError(f"no ticket {ticket_id!r}")
        if ticket.status != TICKET_OPEN:
            raise TicketError(f"ticket {ticket_id!r} is {ticket.status}, not open")
        handle = await self.get(ticket.endpoint_id)
        if ticket.kind == "scale":
            handle.observed["replicas_confirmed"] = True
            await self._save(handle)
        elif ticket.kind == "teardown":
            policy: TeardownMode = (
                "sleep" if handle.observed.get("pending_teardown") == "sleep" else "terminate"
            )
            if policy == "terminate":
                ping = await self._probes.ping(handle.endpoint_id, public_name(handle.endpoint_id))
                if ping.ok:
                    raise TicketError(
                        f"endpoint {handle.endpoint_id} still answers; termination is not applied"
                    )
            handle.observed.pop("pending_teardown", None)
            await self._finish_retire(handle, policy)
        else:
            raise TicketError(f"{ticket.kind} tickets are closed by attach()")
        await self._close_ticket(ticket_id, TICKET_CLOSED, operator)
        return handle

    # -- attach and probe ---------------------------------------------------------------

    async def attach(
        self,
        endpoint_id: str,
        operator_input: Mapping[str, str],
        *,
        probe: bool = True,
        batch_id: str | None = None,
    ) -> EndpointHandle:
        """Assisted mode: accept the operator's base_url, model and deployment id.

        A rejected attempt (``EndpointMismatch``) leaves the endpoint and its ticket untouched,
        so the operator can paste corrected values.
        """
        handle = await self.get(endpoint_id)
        if handle.state not in (S.AWAITING_OPERATOR, S.PROBE_FAILED):
            raise InvalidTransition(
                f"endpoint {endpoint_id}: attach needs awaiting_operator or probe_failed, "
                f"not {handle.state.value}"
            )
        current = handle.state
        attached = await self._provider(handle).attach(handle, operator_input)
        attached.endpoint_id = endpoint_id
        attached.state = current
        deployment_id = operator_input.get("deployment_id")
        if deployment_id:
            attached.external_ids[DEPLOYMENT_ID_KEY] = deployment_id
        operator = operator_input.get("operator")
        for ticket in await self._tickets.list_by_endpoint(endpoint_id):
            if ticket.kind == "deploy" and ticket.status == TICKET_OPEN:
                await self._close_ticket(ticket.ticket_id, TICKET_CLOSED, operator)
        await self._transition(attached, S.VERIFYING, reason="attached", batch_id=batch_id)
        if probe:
            await self.probe(endpoint_id, batch_id=batch_id)
        return await self.get(endpoint_id)

    async def probe(self, endpoint_id: str, *, batch_id: str | None = None) -> ProbeReport:
        """Run P-01..P-15; on success mark READY, store the report and register route + price."""
        handle = await self.get(endpoint_id)
        if handle.state not in _PROBEABLE:
            raise InvalidTransition(
                f"endpoint {endpoint_id}: cannot probe from {handle.state.value}"
            )
        provider = self._provider(handle)
        name = public_name(endpoint_id)
        if handle.state is S.PROBE_FAILED:
            await self._transition(handle, S.VERIFYING, reason="reprobe", batch_id=batch_id)
        current = handle.state
        route = provider.upstream(handle)
        handle.state = current
        spec = handle.model
        await self._gateway.upsert_route(name, route)
        # Priced before the first probe request: the price is frozen by the spec, not by the
        # probe outcome, so probe traffic is costed and the probe key's budget binds. A failed
        # probe removes the route, and the price with it.
        await self._gateway.set_price(name, self._price_for(spec))

        report = await self._probes.run(
            ProbeTarget(
                endpoint_id=endpoint_id,
                model_name=name,
                declared_context_tokens=spec.declared_context_tokens,
                declared_max_output_tokens=spec.declared_max_output_tokens,
                expected_models=frozenset(m for m in (handle.upstream_model, name) if m),
            )
        )
        if await self._reports.get(report.report_id) is None:
            await self._reports.insert(report_to_row(report))
        handle.observed["report_id"] = report.report_id

        failures = probe_failures(report, spec.roles)
        if failures:
            await self._gateway.remove_route(name)
            await self._transition(
                handle,
                S.PROBE_FAILED,
                reason="probe_failed",
                batch_id=batch_id,
                report_id=report.report_id,
                failed_probes=failures,
            )
            return report

        if handle.state in (S.VERIFYING, S.DEGRADED):
            await self._transition(
                handle,
                S.READY,
                reason="probe_passed",
                batch_id=batch_id,
                report_id=report.report_id,
            )
        else:
            await self._emit(
                EventType.ENDPOINT_PROBE_RESULT,
                handle,
                from_state=handle.state,
                reason="reprobe_passed",
                batch_id=batch_id,
                report_id=report.report_id,
            )
            await self._save(handle)
        chat = next((check for check in report.checks if check.probe_id == "P-01"), None)
        cold_ms = chat.data.get("cold_start_ms") if chat is not None else None
        if isinstance(cold_ms, int) and cold_ms > 0:
            await self._record_cold_start(handle, cold_ms / 1000, "probe", batch_id)
        return report

    async def _record_cold_start(
        self, handle: EndpointHandle, cold_start_s: float, source: str, batch_id: str | None
    ) -> None:
        handle.observed["cold_start_s"] = cold_start_s
        await self._emit(
            EventType.ENDPOINT_COLD_START,
            handle,
            from_state=handle.state,
            reason="cold_start",
            batch_id=batch_id,
            cold_start_s=cold_start_s,
            source=source,
        )
        await self._save(handle)

    # -- warm-up --------------------------------------------------------------------------

    async def replicas_required(
        self, endpoint_id: str, *, peak_concurrent_trials: int, requests_per_trial: int
    ) -> int:
        """Replicas needed for a batch, from the plan's ``max_concurrency`` per replica."""
        handle = await self.get(endpoint_id)
        max_concurrency = as_dict(handle.observed.get("plan")).get("max_concurrency")
        if not isinstance(max_concurrency, int) or max_concurrency <= 0:
            raise ValueError(
                f"endpoint {endpoint_id} has no max_concurrency per replica in its plan"
            )
        return required_replicas(
            peak_concurrent_trials=peak_concurrent_trials,
            requests_per_trial=requests_per_trial,
            max_concurrency_per_replica=max_concurrency,
        )

    async def warm(
        self,
        endpoint_ids: Sequence[str],
        replicas_required: int | Mapping[str, int],
        *,
        batch_id: str | None = None,
    ) -> dict[str, WarmResult]:
        """Scale to the required replicas and wake each endpoint until a request succeeds."""

        def needed(endpoint_id: str) -> int:
            if isinstance(replicas_required, int):
                return replicas_required
            return replicas_required[endpoint_id]

        results = await asyncio.gather(
            *(
                self._warm_one(endpoint_id, needed(endpoint_id), batch_id)
                for endpoint_id in endpoint_ids
            )
        )
        return {result.endpoint_id: result for result in results}

    async def _warm_one(self, endpoint_id: str, required: int, batch_id: str | None) -> WarmResult:
        handle = await self.get(endpoint_id)
        if handle.state not in _WARMABLE:
            raise InvalidTransition(
                f"endpoint {endpoint_id}: cannot warm from {handle.state.value}"
            )
        provider = self._provider(handle)
        current = handle.state
        was_sleeping = current in (S.SLEEPING, S.READY)
        confirmed = True
        ticket_id: str | None = None
        if provider.management_mode is not ManagementMode.EXTERNAL:
            plan_max = as_dict(handle.observed.get("plan")).get("replicas_max")
            max_replicas = max(required, plan_max if isinstance(plan_max, int) else required)
            await provider.scale(handle, required, max_replicas)
            handle.state = current  # the manager's state is authoritative, not the provider's
            if provider.management_mode is ManagementMode.ASSISTED:
                ticket = await self._open_ticket(
                    endpoint_id,
                    "scale",
                    f"## Scale {endpoint_id}\n"
                    f"1. Open the deployment `{handle.external_ids.get(DEPLOYMENT_ID_KEY, '?')}` "
                    "in the vendor console.\n"
                    f"2. Set Min Replicas = {required} and Max Replicas = {max_replicas}.",
                )
                confirmed, ticket_id = False, ticket.ticket_id
        handle.observed["replicas_required"] = required
        handle.observed["replicas_confirmed"] = confirmed

        woke = await provider.wake(handle, self._cfg.wake_timeout_s)
        provider_cold_s = handle.observed.get("cold_start_s") if was_sleeping else None
        handle.state = current
        name = public_name(endpoint_id)
        ping = await self._probes.ping(endpoint_id, name, timeout_s=self._cfg.warm_ping_timeout_s)
        if woke is not S.WARM or not ping.ok:
            reason = "wake_timeout" if woke is not S.WARM else "health_request_failed"
            if current is not S.SLEEPING:
                await self._transition(
                    handle,
                    S.SLEEPING if woke is not S.WARM else S.DEGRADED,
                    reason=reason,
                    batch_id=batch_id,
                    event_type=EventType.ENDPOINT_UNAVAILABLE,
                    status=ping.status,
                    error=ping.error,
                )
            else:
                await self._save(handle)
            return WarmResult(endpoint_id, handle.state, required, confirmed, None, ticket_id)

        cold_s = max(
            ping.cold_start_ms / 1000,
            float(provider_cold_s) if isinstance(provider_cold_s, (int, float)) else 0.0,
            ping.latency_s if ping.latency_s > self._cfg.cold_start_threshold_s else 0.0,
        )
        if cold_s > 0:
            await self._record_cold_start(handle, cold_s, "wake", batch_id)
        if current is not S.WARM:
            await self._transition(handle, S.WARM, reason="warmed", batch_id=batch_id)
        else:
            await self._save(handle)
        return WarmResult(endpoint_id, handle.state, required, confirmed, cold_s or None, ticket_id)

    # -- batch guards ---------------------------------------------------------------------

    async def freeze_guard(self, endpoint_ids: Iterable[str], *, planned_end: date) -> GuardReport:
        """Freeze needs every endpoint READY and every HF token valid past ``planned_end`` + 7 d."""
        today = datetime.fromtimestamp(self._clock.now(), UTC).date()
        blockers: list[str] = []
        warnings: list[str] = []
        for endpoint_id in endpoint_ids:
            handle = await self.get(endpoint_id)
            if handle.state not in _USABLE:
                blockers.append(f"{endpoint_id}: state is {handle.state.value}, expected ready")
            source = handle.model.source
            if isinstance(source, HFSource) and source.token_ref is not None:
                expiry = check_token_expiry(
                    expires_at=self._hf_token_expiry.get(source.token_ref),
                    today=today,
                    batch_end=planned_end,
                )
                if not expiry.ok:
                    blockers.append(f"{endpoint_id}: HF token {source.token_ref}: {expiry.reason}")
                elif expiry.warning:
                    warnings.append(f"{endpoint_id}: HF token {source.token_ref}: {expiry.reason}")
        return GuardReport(not blockers, tuple(blockers), tuple(warnings))

    async def running_guard(self, endpoint_ids: Iterable[str]) -> GuardReport:
        """Running needs every endpoint WARM at the required replica count."""
        blockers: list[str] = []
        for endpoint_id in endpoint_ids:
            handle = await self.get(endpoint_id)
            if handle.state is not S.WARM:
                blockers.append(f"{endpoint_id}: state is {handle.state.value}, expected warm")
            elif handle.observed.get("replicas_confirmed") is not True:
                blockers.append(
                    f"{endpoint_id}: {handle.observed.get('replicas_required')} replicas "
                    "not confirmed"
                )
        return GuardReport(not blockers, tuple(blockers))

    # -- health and idle spin -----------------------------------------------------------

    async def health_check(self, endpoint_id: str, *, batch_id: str) -> PingResult | None:
        """One ``max_tokens=1`` request; only call this while a batch is running."""
        handle = await self.get(endpoint_id)
        if handle.state not in _HEALTH_CHECKED:
            return None
        ping = await self._probes.ping(endpoint_id, public_name(endpoint_id))
        if ping.ok:
            if ping.cold_start_ms > 0:
                await self._record_cold_start(handle, ping.cold_start_ms / 1000, "health", batch_id)
            if handle.state is not S.WARM:
                await self._transition(handle, S.WARM, reason="health_ok", batch_id=batch_id)
        elif handle.state in (S.READY, S.WARM):
            await self._transition(
                handle,
                S.DEGRADED,
                reason="health_failed",
                batch_id=batch_id,
                status=ping.status,
                error=ping.error,
            )
        return ping

    async def record_traffic(self, endpoint_id: str) -> None:
        """Note real traffic on an endpoint; resets the idle-spin clock."""
        handle = await self.get(endpoint_id)
        handle.observed["last_traffic_at"] = self._clock.now()
        handle.observed.pop("idle_alerted", None)
        await self._save(handle)

    async def check_idle(self) -> list[IdleSpinAlert]:
        """Alert once for each WARM endpoint idle longer than the threshold (billing continues)."""
        now = self._clock.now()
        alerts: list[IdleSpinAlert] = []
        for row in await self._endpoints.list_by_state(S.WARM.value):
            observed = row.observed
            last = max(
                float(observed.get("last_traffic_at") or 0.0),
                float(observed.get("warm_since") or 0.0),
            )
            if now - last < self._cfg.idle_threshold_s or observed.get("idle_alerted"):
                continue
            alert = IdleSpinAlert(row.endpoint_id, now - last)
            if self._idle_alert is not None:
                await self._idle_alert(alert)
            handle = await self.get(row.endpoint_id)
            handle.observed["idle_alerted"] = True
            await self._save(handle)
            alerts.append(alert)
        return alerts

    # -- retirement -------------------------------------------------------------------------

    async def retire(
        self, endpoint_id: str, policy: TeardownMode, *, batch_id: str | None = None
    ) -> EndpointHandle:
        """Scale to zero (``sleep``) or shut down (``terminate``) after the batch is graded.

        Assisted endpoints open a teardown ticket instead and change state when it is confirmed.
        """
        handle = await self.get(endpoint_id)
        if handle.state in (S.RETIRED, S.REJECTED):
            raise InvalidTransition(f"endpoint {endpoint_id} is already {handle.state.value}")
        provider = self._provider(handle)
        current = handle.state
        await provider.teardown(handle, policy)
        handle.state = current
        if provider.management_mode is ManagementMode.ASSISTED:
            action = (
                "Set Min Replicas to 0 (scale to zero)"
                if policy == "sleep"
                else "Terminate the deployment"
            )
            ticket = await self._open_ticket(
                endpoint_id,
                "teardown",
                f"## Retire {endpoint_id} ({policy})\n"
                f"1. Open deployment `{handle.external_ids.get(DEPLOYMENT_ID_KEY, '?')}` in the "
                f"vendor console.\n2. {action}.",
            )
            handle.observed["pending_teardown"] = policy
            handle.observed["teardown_ticket_id"] = ticket.ticket_id
            await self._save(handle)
            return handle
        await self._finish_retire(handle, policy, batch_id=batch_id)
        return handle

    async def _finish_retire(
        self, handle: EndpointHandle, policy: TeardownMode, *, batch_id: str | None = None
    ) -> None:
        external = self._provider(handle).management_mode is ManagementMode.EXTERNAL
        final = S.RETIRED if policy == "terminate" or external else S.SLEEPING
        await self._transition(
            handle, final, reason=f"retire:{policy}", batch_id=batch_id, policy=policy
        )
        if final is S.RETIRED:
            await self._gateway.remove_route(public_name(handle.endpoint_id))
