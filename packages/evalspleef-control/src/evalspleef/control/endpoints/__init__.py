"""Endpoint manager (TECH-DESIGN section 6.2): lifecycle, tickets, warm-up, health, retirement."""

from .engine_logs import EngineLogStore
from .health import HealthLoop, RunningBatches
from .manager import (
    EndpointManager,
    EndpointManagerConfig,
    GuardReport,
    IdleSpinAlert,
    WarmResult,
    probe_failures,
    required_replicas,
)
from .states import (
    TRANSITIONS,
    EndpointNotFound,
    InvalidTransition,
    TicketError,
    event_type_for,
    make_endpoint_id,
    public_name,
)
from .tickets import ticket_expires_at, ticket_text

__all__ = [
    "TRANSITIONS",
    "EndpointManager",
    "EndpointManagerConfig",
    "EndpointNotFound",
    "EngineLogStore",
    "GuardReport",
    "HealthLoop",
    "IdleSpinAlert",
    "InvalidTransition",
    "RunningBatches",
    "TicketError",
    "WarmResult",
    "event_type_for",
    "make_endpoint_id",
    "probe_failures",
    "public_name",
    "required_replicas",
    "ticket_expires_at",
    "ticket_text",
]
