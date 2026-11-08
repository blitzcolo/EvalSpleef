"""Append-only event store for the control plane (PRD appendix B.8).

The event log is authoritative; every state change must be accompanied by an
event and database rows are projections (AGENTS.md architecture rule 6).

* :class:`JsonlEventStore` — one append-only file per batch, optional fsync,
  sequence numbers recoverable from the file.
* :class:`SqlEventStore` — relational table through :mod:`evalspleef.control.store`.
* :func:`open_event_store` — choose by URL (``jsonl:///dir``,
  ``sqlite:///path``, ``postgresql://...``).
* :func:`project_trial_state` / :func:`verify_projection` — replay the core
  trial state machine from events and check the relational projection.
"""

from .base import EventStore, EventStoreClosed, EventStoreError
from .factory import open_event_store
from .jsonl import JsonlEventStore
from .project import (
    MalformedTransition,
    ProjectionMismatch,
    ProjectionReport,
    project_trial_state,
    replay_trial_machine,
    verify_projection,
)
from .sql import SqlEventStore

__all__ = [
    "EventStore",
    "EventStoreClosed",
    "EventStoreError",
    "JsonlEventStore",
    "MalformedTransition",
    "ProjectionMismatch",
    "ProjectionReport",
    "SqlEventStore",
    "open_event_store",
    "project_trial_state",
    "replay_trial_machine",
    "verify_projection",
]
