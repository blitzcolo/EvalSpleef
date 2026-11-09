"""Relational result store for the control plane (TECH-DESIGN section 11).

Portable SQLite/PostgreSQL persistence: :func:`connect` opens a
:class:`Database`, :func:`migrate` applies the SQL files in ``migrations/``,
and the repository classes in :mod:`evalspleef.control.store.repos` give typed
access to every table. JSON documents travel as TEXT, booleans as INTEGER,
timestamps as ISO-8601 UTC strings.

Events stay authoritative: every state change is written to the event store
first, and these tables are projections (see :mod:`evalspleef.control.events`).
"""

from .db import Database, PostgresDatabase, SqliteDatabase, connect
from .migrations import migrate
from .repos import (
    ArtifactRepository,
    BenchmarkRepository,
    CanaryRepository,
    EndpointRepository,
    EndpointTicketRepository,
    GatewayKeyRepository,
    GradeRepository,
    HfArtifactRepository,
    HostRepository,
    IncidentRepository,
    ModelSpecExistsError,
    ModelSpecRepository,
    ProbeReportRepository,
    ProviderInstanceRepository,
    RoundRepository,
    SandboxLeaseRepository,
    TemplateBuildRepository,
    TrialRepository,
)
from .rows import (
    ArtifactRow,
    BenchmarkRow,
    CanaryRow,
    EndpointRow,
    EndpointTicketRow,
    GatewayKeyRow,
    GradeRow,
    HfArtifactRow,
    HostRow,
    IncidentRow,
    ModelSpecRow,
    ProbeReportRow,
    ProviderInstanceRow,
    RoundRow,
    SandboxLeaseRow,
    TemplateBuildRow,
    TrialRow,
)

__all__ = [
    "ArtifactRepository",
    "ArtifactRow",
    "BenchmarkRepository",
    "BenchmarkRow",
    "CanaryRepository",
    "CanaryRow",
    "Database",
    "EndpointRepository",
    "EndpointRow",
    "EndpointTicketRepository",
    "EndpointTicketRow",
    "GatewayKeyRepository",
    "GatewayKeyRow",
    "GradeRepository",
    "GradeRow",
    "HfArtifactRepository",
    "HfArtifactRow",
    "HostRepository",
    "HostRow",
    "IncidentRepository",
    "IncidentRow",
    "ModelSpecExistsError",
    "ModelSpecRepository",
    "ModelSpecRow",
    "PostgresDatabase",
    "ProbeReportRepository",
    "ProbeReportRow",
    "ProviderInstanceRepository",
    "ProviderInstanceRow",
    "RoundRepository",
    "RoundRow",
    "SandboxLeaseRepository",
    "SandboxLeaseRow",
    "SqliteDatabase",
    "TemplateBuildRepository",
    "TemplateBuildRow",
    "TrialRepository",
    "TrialRow",
    "connect",
    "migrate",
]
