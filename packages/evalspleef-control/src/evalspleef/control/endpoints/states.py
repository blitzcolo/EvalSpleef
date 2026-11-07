"""Endpoint lifecycle table (TECH-DESIGN section 6.2), event mapping and naming rules.

``core.events`` only defines six ``endpoint.*`` event types, so lifecycle steps share them and
carry the exact state in the payload (``from_state`` / ``state``):

* ``endpoint.registered``: registered, awaiting_operator, deploying, verifying
* ``endpoint.probe.result``: ready, probe_failed (and re-probes that keep the state)
* ``endpoint.warm``: warm
* ``endpoint.cold_start``: a measured cold start (not a state change)
* ``endpoint.unavailable``: rejected, failed, degraded, a failed wake-up
* ``endpoint.retired``: sleeping, retired
"""

from __future__ import annotations

import hashlib

from evalspleef.core.events import EventType
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.model import EndpointState, ModelSpec

__all__ = [
    "TRANSITIONS",
    "EndpointNotFound",
    "InvalidTransition",
    "TicketError",
    "event_type_for",
    "make_endpoint_id",
    "public_name",
]

S = EndpointState

TRANSITIONS: dict[EndpointState, frozenset[EndpointState]] = {
    S.REGISTERED: frozenset({S.REJECTED, S.AWAITING_OPERATOR, S.DEPLOYING, S.VERIFYING, S.FAILED}),
    S.REJECTED: frozenset({S.REGISTERED}),
    S.AWAITING_OPERATOR: frozenset({S.VERIFYING, S.FAILED, S.RETIRED}),
    S.DEPLOYING: frozenset({S.VERIFYING, S.FAILED, S.RETIRED}),
    S.VERIFYING: frozenset({S.READY, S.PROBE_FAILED, S.FAILED, S.RETIRED}),
    S.PROBE_FAILED: frozenset({S.VERIFYING, S.FAILED, S.RETIRED}),
    S.READY: frozenset({S.WARM, S.SLEEPING, S.DEGRADED, S.PROBE_FAILED, S.FAILED, S.RETIRED}),
    S.WARM: frozenset({S.READY, S.SLEEPING, S.DEGRADED, S.PROBE_FAILED, S.FAILED, S.RETIRED}),
    S.SLEEPING: frozenset({S.WARM, S.DEGRADED, S.FAILED, S.RETIRED}),
    S.DEGRADED: frozenset({S.WARM, S.READY, S.SLEEPING, S.PROBE_FAILED, S.FAILED, S.RETIRED}),
    S.FAILED: frozenset({S.RETIRED}),
    S.RETIRED: frozenset(),
}

_EVENT_FOR_STATE: dict[EndpointState, EventType] = {
    S.REGISTERED: EventType.ENDPOINT_REGISTERED,
    S.AWAITING_OPERATOR: EventType.ENDPOINT_REGISTERED,
    S.DEPLOYING: EventType.ENDPOINT_REGISTERED,
    S.VERIFYING: EventType.ENDPOINT_REGISTERED,
    S.READY: EventType.ENDPOINT_PROBE_RESULT,
    S.PROBE_FAILED: EventType.ENDPOINT_PROBE_RESULT,
    S.WARM: EventType.ENDPOINT_WARM,
    S.SLEEPING: EventType.ENDPOINT_RETIRED,
    S.RETIRED: EventType.ENDPOINT_RETIRED,
    S.REJECTED: EventType.ENDPOINT_UNAVAILABLE,
    S.FAILED: EventType.ENDPOINT_UNAVAILABLE,
    S.DEGRADED: EventType.ENDPOINT_UNAVAILABLE,
}


class EndpointNotFound(SpleefError):
    """No endpoint (or its model spec) is stored under the given id."""

    error_class = "version"
    disposition = "manual"


class InvalidTransition(SpleefError):
    """The requested operation does not apply to the endpoint's current state."""

    error_class = "version"
    disposition = "manual"


class TicketError(SpleefError):
    """An operator ticket is unknown, already closed, or its confirmation does not hold."""

    error_class = "environment"
    disposition = "manual"


def event_type_for(state: EndpointState) -> EventType:
    return _EVENT_FOR_STATE[state]


def make_endpoint_id(spec: ModelSpec) -> str:
    """``ep_<slug>_<8 hex>``: deterministic per ``slug`` and ``spec_version`` (TECH-DESIGN 6.1)."""
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


def public_name(endpoint_id: str) -> str:
    """Model name on the gateway: ``ep_<endpoint_id>``, never double-prefixed."""
    return endpoint_id if endpoint_id.startswith("ep_") else f"ep_{endpoint_id}"
