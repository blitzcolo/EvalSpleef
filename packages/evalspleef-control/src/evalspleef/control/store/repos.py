"""Typed repositories for the control-plane relational store.

One repository per table (TECH-DESIGN section 11 plus the v2 tables). Write
methods are explicit about mutability: ``model_spec`` and ``probe_report`` rows
are immutable and reject duplicates, most registry tables upsert, and versioned
tables (``grade``, ``round``) append. All methods are async and take a
:class:`~evalspleef.control.store.db.Database`.
"""

from __future__ import annotations

from .db import INTEGRITY_ERRORS, Database
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
    "BenchmarkRepository",
    "CanaryRepository",
    "EndpointRepository",
    "EndpointTicketRepository",
    "GatewayKeyRepository",
    "GradeRepository",
    "HfArtifactRepository",
    "HostRepository",
    "IncidentRepository",
    "ModelSpecExistsError",
    "ModelSpecRepository",
    "ProbeReportRepository",
    "ProviderInstanceRepository",
    "RoundRepository",
    "SandboxLeaseRepository",
    "TemplateBuildRepository",
    "TrialRepository",
]


class ModelSpecExistsError(ValueError):
    """Updating an existing ``(slug, spec_version)`` row: model specs are immutable."""


def _upsert_sql(table: str, columns: tuple[str, ...], pk: str) -> str:
    """Portable upsert (SQLite >= 3.24 and PostgreSQL both support ON CONFLICT)."""
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col != pk)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
    )


class ModelSpecRepository:
    """Immutable model spec versions; any change is a new ``spec_version``."""

    _INSERT = (
        "INSERT INTO model_spec (slug, spec_version, family, roles, source, deployment, "
        "declared_context_tokens, declared_max_output_tokens, pricing_ref, license, owner, "
        "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: ModelSpecRow) -> None:
        """Insert a new spec version; a duplicate primary key raises instead of updating."""
        try:
            await self._db.execute(self._INSERT, row.params())
        except INTEGRITY_ERRORS as exc:
            raise ModelSpecExistsError(
                f"model spec {row.slug!r} version {row.spec_version} already exists; "
                "model specs are immutable, register a new spec_version"
            ) from exc

    async def get(self, slug: str, spec_version: int) -> ModelSpecRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM model_spec WHERE slug = %s AND spec_version = %s",
            (slug, spec_version),
        )
        return ModelSpecRow.from_row(row) if row else None

    async def list_by_slug(self, slug: str) -> list[ModelSpecRow]:
        rows = await self._db.fetch(
            "SELECT * FROM model_spec WHERE slug = %s ORDER BY spec_version", (slug,)
        )
        return [ModelSpecRow.from_row(row) for row in rows]


class HfArtifactRepository:
    """Frozen HF mirror records."""

    _COLUMNS = (
        "repo",
        "revision",
        "mirror_repo",
        "files_sha256",
        "safetensors_bytes",
        "license",
        "verified_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: HfArtifactRow) -> None:
        await self._db.execute(
            _upsert_sql("hf_artifact", self._COLUMNS, "repo, revision"), row.params()
        )

    async def get(self, repo: str, revision: str) -> HfArtifactRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM hf_artifact WHERE repo = %s AND revision = %s", (repo, revision)
        )
        return HfArtifactRow.from_row(row) if row else None


class EndpointRepository:
    """Endpoint lifecycle projection; the event log stays authoritative."""

    _COLUMNS = (
        "endpoint_id",
        "model_slug",
        "model_spec_version",
        "adapter",
        "account",
        "state",
        "external_ids",
        "observed",
        "created_at",
        "retired_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: EndpointRow) -> None:
        await self._db.execute(_upsert_sql("endpoint", self._COLUMNS, "endpoint_id"), row.params())

    async def get(self, endpoint_id: str) -> EndpointRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM endpoint WHERE endpoint_id = %s", (endpoint_id,)
        )
        return EndpointRow.from_row(row) if row else None

    async def update_state(
        self, endpoint_id: str, state: str, *, retired_at: str | None = None
    ) -> None:
        await self._db.execute(
            "UPDATE endpoint SET state = %s, retired_at = %s WHERE endpoint_id = %s",
            (state, retired_at, endpoint_id),
        )

    async def list_by_state(self, state: str) -> list[EndpointRow]:
        rows = await self._db.fetch(
            "SELECT * FROM endpoint WHERE state = %s ORDER BY endpoint_id", (state,)
        )
        return [EndpointRow.from_row(row) for row in rows]


class EndpointTicketRepository:
    """Assisted-mode operator tickets."""

    _COLUMNS = (
        "ticket_id",
        "endpoint_id",
        "kind",
        "instructions_md",
        "status",
        "operator",
        "opened_at",
        "closed_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: EndpointTicketRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO endpoint_ticket (ticket_id, endpoint_id, kind, instructions_md, "
                "status, operator, opened_at, closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"endpoint ticket {row.ticket_id!r} already exists") from exc

    async def get(self, ticket_id: str) -> EndpointTicketRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM endpoint_ticket WHERE ticket_id = %s", (ticket_id,)
        )
        return EndpointTicketRow.from_row(row) if row else None

    async def update_status(
        self,
        ticket_id: str,
        status: str,
        *,
        operator: str | None = None,
        closed_at: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE endpoint_ticket SET status = %s, operator = %s, closed_at = %s "
            "WHERE ticket_id = %s",
            (status, operator, closed_at, ticket_id),
        )

    async def list_by_endpoint(self, endpoint_id: str) -> list[EndpointTicketRow]:
        rows = await self._db.fetch(
            "SELECT * FROM endpoint_ticket WHERE endpoint_id = %s ORDER BY opened_at",
            (endpoint_id,),
        )
        return [EndpointTicketRow.from_row(row) for row in rows]


class ProbeReportRepository:
    """Immutable probe reports; freeze-time references them by id."""

    _COLUMNS = (
        "report_id",
        "endpoint_id",
        "gateway_version",
        "results",
        "capabilities",
        "created_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: ProbeReportRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO probe_report (report_id, endpoint_id, gateway_version, results, "
                "capabilities, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"probe report {row.report_id!r} already exists") from exc

    async def get(self, report_id: str) -> ProbeReportRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM probe_report WHERE report_id = %s", (report_id,)
        )
        return ProbeReportRow.from_row(row) if row else None

    async def list_by_endpoint(self, endpoint_id: str) -> list[ProbeReportRow]:
        rows = await self._db.fetch(
            "SELECT * FROM probe_report WHERE endpoint_id = %s ORDER BY created_at", (endpoint_id,)
        )
        return [ProbeReportRow.from_row(row) for row in rows]


class BenchmarkRepository:
    """Immutable inference benchmarks; a batch release reads the latest per endpoint."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: BenchmarkRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO endpoint_benchmark (bench_id, endpoint_id, created_at, plan, facts, "
                "result, raw_path, raw_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"benchmark {row.bench_id!r} already exists") from exc

    async def get(self, bench_id: str) -> BenchmarkRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM endpoint_benchmark WHERE bench_id = %s", (bench_id,)
        )
        return BenchmarkRow.from_row(row) if row else None

    async def list_by_endpoint(self, endpoint_id: str) -> list[BenchmarkRow]:
        rows = await self._db.fetch(
            "SELECT * FROM endpoint_benchmark WHERE endpoint_id = %s ORDER BY created_at, bench_id",
            (endpoint_id,),
        )
        return [BenchmarkRow.from_row(row) for row in rows]

    async def latest(self, endpoint_id: str) -> BenchmarkRow | None:
        rows = await self.list_by_endpoint(endpoint_id)
        return rows[-1] if rows else None


class ProviderInstanceRepository:
    """Capability declarations and archived isolation profiles."""

    _COLUMNS = (
        "instance",
        "adapter",
        "version",
        "capabilities",
        "isolation_profile",
        "verified_isolation",
        "config_hash",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: ProviderInstanceRow) -> None:
        await self._db.execute(
            _upsert_sql("provider_instance", self._COLUMNS, "instance"), row.params()
        )

    async def get(self, instance: str) -> ProviderInstanceRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM provider_instance WHERE instance = %s", (instance,)
        )
        return ProviderInstanceRow.from_row(row) if row else None

    async def list_all(self) -> list[ProviderInstanceRow]:
        rows = await self._db.fetch("SELECT * FROM provider_instance ORDER BY instance")
        return [ProviderInstanceRow.from_row(row) for row in rows]


class HostRepository:
    """Local host inventory."""

    _COLUMNS = ("host_id", "instance", "os", "hw", "hostd_version", "state", "last_heartbeat")

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: HostRow) -> None:
        await self._db.execute(_upsert_sql("host", self._COLUMNS, "host_id"), row.params())

    async def get(self, host_id: str) -> HostRow | None:
        row = await self._db.fetch_one("SELECT * FROM host WHERE host_id = %s", (host_id,))
        return HostRow.from_row(row) if row else None

    async def update_state(
        self, host_id: str, state: str, *, last_heartbeat: str | None = None
    ) -> None:
        await self._db.execute(
            "UPDATE host SET state = %s, last_heartbeat = %s WHERE host_id = %s",
            (state, last_heartbeat, host_id),
        )

    async def list_all(self) -> list[HostRow]:
        rows = await self._db.fetch("SELECT * FROM host ORDER BY host_id")
        return [HostRow.from_row(row) for row in rows]


class TemplateBuildRepository:
    """Template registry."""

    _COLUMNS = (
        "env_id",
        "env_version",
        "oci_digest",
        "provider_template_ids",
        "fingerprints",
        "status",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: TemplateBuildRow) -> None:
        await self._db.execute(
            _upsert_sql("template_build", self._COLUMNS, "env_id, env_version"), row.params()
        )

    async def get(self, env_id: str, env_version: str) -> TemplateBuildRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM template_build WHERE env_id = %s AND env_version = %s",
            (env_id, env_version),
        )
        return TemplateBuildRow.from_row(row) if row else None

    async def list_by_env(self, env_id: str) -> list[TemplateBuildRow]:
        rows = await self._db.fetch(
            "SELECT * FROM template_build WHERE env_id = %s ORDER BY env_version", (env_id,)
        )
        return [TemplateBuildRow.from_row(row) for row in rows]


class SandboxLeaseRepository:
    """Sandbox leases; every trial holds at least an agent and a verifier lease."""

    _COLUMNS = (
        "lease_id",
        "batch_id",
        "trial_id",
        "role",
        "provider_instance",
        "host_id",
        "template_kind",
        "template_ref",
        "template_digest",
        "created_at",
        "destroyed_at",
        "reaped",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: SandboxLeaseRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO sandbox_lease (lease_id, batch_id, trial_id, role, provider_instance, "
                "host_id, template_kind, template_ref, template_digest, created_at, destroyed_at, "
                "reaped) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"sandbox lease {row.lease_id!r} already exists") from exc

    async def get(self, lease_id: str) -> SandboxLeaseRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM sandbox_lease WHERE lease_id = %s", (lease_id,)
        )
        return SandboxLeaseRow.from_row(row) if row else None

    async def list_by_trial(self, trial_id: str) -> list[SandboxLeaseRow]:
        rows = await self._db.fetch(
            "SELECT * FROM sandbox_lease WHERE trial_id = %s ORDER BY created_at", (trial_id,)
        )
        return [SandboxLeaseRow.from_row(row) for row in rows]

    async def mark_destroyed(
        self, lease_id: str, destroyed_at: str, *, reaped: bool = False
    ) -> None:
        await self._db.execute(
            "UPDATE sandbox_lease SET destroyed_at = %s, reaped = %s WHERE lease_id = %s",
            (destroyed_at, 1 if reaped else 0, lease_id),
        )


class GatewayKeyRepository:
    """Per-trial gateway key metadata (never the key material itself)."""

    _COLUMNS = ("key_id", "trial_id", "endpoint_id", "budget", "expires_at", "revoked_at")

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: GatewayKeyRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO gateway_key (key_id, trial_id, endpoint_id, budget, expires_at, "
                "revoked_at) VALUES (%s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"gateway key {row.key_id!r} already exists") from exc

    async def get(self, key_id: str) -> GatewayKeyRow | None:
        row = await self._db.fetch_one("SELECT * FROM gateway_key WHERE key_id = %s", (key_id,))
        return GatewayKeyRow.from_row(row) if row else None

    async def revoke(self, key_id: str, revoked_at: str) -> None:
        await self._db.execute(
            "UPDATE gateway_key SET revoked_at = %s WHERE key_id = %s", (revoked_at, key_id)
        )


class TrialRepository:
    """Trial projection rows (state changes are event-driven; see ``events``)."""

    _COLUMNS = (
        "trial_id",
        "batch_id",
        "cell",
        "task_id",
        "model",
        "shell",
        "state",
        "validity",
        "epoch",
        "idx",
        "extensions",
        "created_at",
        "updated_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: TrialRow) -> None:
        await self._db.execute(_upsert_sql("trial", self._COLUMNS, "trial_id"), row.params())

    async def get(self, trial_id: str) -> TrialRow | None:
        row = await self._db.fetch_one("SELECT * FROM trial WHERE trial_id = %s", (trial_id,))
        return TrialRow.from_row(row) if row else None

    async def update_state(
        self,
        trial_id: str,
        state: str,
        *,
        validity: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        """Project a state change; ``validity``/``updated_at`` default to keeping the row's values."""
        await self._db.execute(
            "UPDATE trial SET state = %s, "
            "validity = COALESCE(%s, validity), "
            "updated_at = COALESCE(%s, updated_at) "
            "WHERE trial_id = %s",
            (state, validity, updated_at, trial_id),
        )

    async def list_by_batch(self, batch_id: str) -> list[TrialRow]:
        rows = await self._db.fetch(
            "SELECT * FROM trial WHERE batch_id = %s ORDER BY trial_id", (batch_id,)
        )
        return [TrialRow.from_row(row) for row in rows]


class GradeRepository:
    """Grade versions; regrading and overrides append a new version, never overwrite."""

    _COLUMNS = (
        "trial_id",
        "version",
        "tier",
        "status",
        "decided_by",
        "rule_id",
        "payload",
        "created_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: GradeRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO grade (trial_id, version, tier, status, decided_by, rule_id, payload, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(
                f"grade version {row.version} already exists for trial {row.trial_id!r}"
            ) from exc

    async def get(self, trial_id: str, version: int) -> GradeRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM grade WHERE trial_id = %s AND version = %s", (trial_id, version)
        )
        return GradeRow.from_row(row) if row else None

    async def list_by_trial(self, trial_id: str) -> list[GradeRow]:
        rows = await self._db.fetch(
            "SELECT * FROM grade WHERE trial_id = %s ORDER BY version", (trial_id,)
        )
        return [GradeRow.from_row(row) for row in rows]

    async def current(self, trial_id: str) -> GradeRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM grade WHERE trial_id = %s ORDER BY version DESC LIMIT 1", (trial_id,)
        )
        return GradeRow.from_row(row) if row else None


class RoundRepository:
    """Agent rounds of a trial."""

    _COLUMNS = ("trial_id", "round_no", "started_at", "stopped_at", "agent_steps", "payload")

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, row: RoundRow) -> None:
        await self._db.execute(
            _upsert_sql("round", self._COLUMNS, "trial_id, round_no"), row.params()
        )

    async def list_by_trial(self, trial_id: str) -> list[RoundRow]:
        rows = await self._db.fetch(
            "SELECT * FROM round WHERE trial_id = %s ORDER BY round_no", (trial_id,)
        )
        return [RoundRow.from_row(row) for row in rows]

    async def finish(
        self, trial_id: str, round_no: int, *, stopped_at: str, agent_steps: int
    ) -> None:
        await self._db.execute(
            "UPDATE round SET stopped_at = %s, agent_steps = %s "
            "WHERE trial_id = %s AND round_no = %s",
            (stopped_at, agent_steps, trial_id, round_no),
        )


class ArtifactRepository:
    """Trial artifact index."""

    _COLUMNS = ("artifact_id", "trial_id", "kind", "path", "sha256", "size_bytes", "created_at")

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: ArtifactRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO artifact (artifact_id, trial_id, kind, path, sha256, size_bytes, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"artifact {row.artifact_id!r} already exists") from exc

    async def get(self, artifact_id: str) -> ArtifactRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM artifact WHERE artifact_id = %s", (artifact_id,)
        )
        return ArtifactRow.from_row(row) if row else None

    async def list_by_trial(self, trial_id: str) -> list[ArtifactRow]:
        rows = await self._db.fetch(
            "SELECT * FROM artifact WHERE trial_id = %s ORDER BY created_at", (trial_id,)
        )
        return [ArtifactRow.from_row(row) for row in rows]


class IncidentRepository:
    """Incidents with their error classification and disposition."""

    _COLUMNS = (
        "incident_id",
        "trial_id",
        "error_class",
        "disposition",
        "detail",
        "opened_at",
        "closed_at",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: IncidentRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO incident (incident_id, trial_id, error_class, disposition, detail, "
                "opened_at, closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"incident {row.incident_id!r} already exists") from exc

    async def get(self, incident_id: str) -> IncidentRow | None:
        row = await self._db.fetch_one(
            "SELECT * FROM incident WHERE incident_id = %s", (incident_id,)
        )
        return IncidentRow.from_row(row) if row else None

    async def close(self, incident_id: str, closed_at: str) -> None:
        await self._db.execute(
            "UPDATE incident SET closed_at = %s WHERE incident_id = %s", (closed_at, incident_id)
        )

    async def list_open(self) -> list[IncidentRow]:
        rows = await self._db.fetch(
            "SELECT * FROM incident WHERE closed_at IS NULL ORDER BY opened_at"
        )
        return [IncidentRow.from_row(row) for row in rows]


class CanaryRepository:
    """Canary token injections and their scan results."""

    _COLUMNS = ("canary_id", "trial_id", "token", "injected_at", "scanned_at", "hits")

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(self, row: CanaryRow) -> None:
        try:
            await self._db.execute(
                "INSERT INTO canary (canary_id, trial_id, token, injected_at, scanned_at, hits) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                row.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"canary {row.canary_id!r} already exists") from exc

    async def get(self, canary_id: str) -> CanaryRow | None:
        row = await self._db.fetch_one("SELECT * FROM canary WHERE canary_id = %s", (canary_id,))
        return CanaryRow.from_row(row) if row else None

    async def list_by_trial(self, trial_id: str) -> list[CanaryRow]:
        rows = await self._db.fetch(
            "SELECT * FROM canary WHERE trial_id = %s ORDER BY injected_at", (trial_id,)
        )
        return [CanaryRow.from_row(row) for row in rows]

    async def mark_scanned(self, canary_id: str, scanned_at: str, *, hits: int) -> None:
        await self._db.execute(
            "UPDATE canary SET scanned_at = %s, hits = %s WHERE canary_id = %s",
            (scanned_at, hits, canary_id),
        )
