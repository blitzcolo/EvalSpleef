"""Operator tickets for assisted endpoints (TECH-DESIGN section 6.6.2).

A ticket is an ``endpoint_ticket`` row: the console steps an operator must carry out by hand.
Deploy tickets expire after 24 hours and fail the endpoint.
"""

from __future__ import annotations

from datetime import datetime

from evalspleef.control.probes.clock import iso_utc
from evalspleef.control.store.rows import EndpointTicketRow

__all__ = [
    "TICKET_CLOSED",
    "TICKET_EXPIRED",
    "TICKET_OPEN",
    "ticket_expires_at",
    "ticket_text",
]

TICKET_OPEN = "open"
TICKET_CLOSED = "closed"
TICKET_EXPIRED = "expired"

_FOLLOW_UP = {
    "deploy": (
        "When the deployment is RUNNING, copy base_url and model from Quick Start and run:\n"
        "  spleef models attach {endpoint_id} --base-url <...> --model <...> "
        "--deployment-id <...>"
    ),
    "scale": "Confirm ticket {ticket_id} once the replica settings are applied.",
    "teardown": "Confirm ticket {ticket_id} once the action is done; the system re-checks it.",
}


def ticket_expires_at(ticket: EndpointTicketRow, ttl_s: float) -> float:
    """Epoch seconds at which an open ticket expires."""
    return datetime.fromisoformat(ticket.opened_at).timestamp() + ttl_s


def ticket_text(ticket: EndpointTicketRow, ttl_s: float) -> str:
    """Plain-text rendering the CLI prints for the operator."""
    follow_up = _FOLLOW_UP.get(ticket.kind, "").format(
        endpoint_id=ticket.endpoint_id, ticket_id=ticket.ticket_id
    )
    lines = [
        f"Operator ticket {ticket.ticket_id} ({ticket.kind})",
        f"Endpoint: {ticket.endpoint_id}",
        f"Opened:   {ticket.opened_at}",
        f"Expires:  {iso_utc(ticket_expires_at(ticket, ttl_s))}",
        f"Status:   {ticket.status}",
        "",
        ticket.instructions_md.strip(),
    ]
    if follow_up:
        lines += ["", follow_up]
    return "\n".join(lines) + "\n"
