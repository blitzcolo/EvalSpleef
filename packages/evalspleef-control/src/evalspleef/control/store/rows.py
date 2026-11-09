"""Typed row models for the control-plane relational store.

Each frozen dataclass mirrors one table of TECH-DESIGN section 11 (plus the v2
tables) and converts to/from the portable storage representation: JSON
documents as ``dict``/TEXT, booleans as bool/INTEGER, timestamps as ISO-8601
UTC strings. Repositories translate between these rows and SQL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Self

from .db import Row

__all__ = [
    "ArtifactRow",
    "CanaryRow",
    "EndpointRow",
    "EndpointTicketRow",
    "GradeRow",
    "HfArtifactRow",
    "HostRow",
    "IncidentRow",
    "ModelSpecRow",
    "ProbeReportRow",
    "ProviderInstanceRow",
    "RoundRow",
    "SandboxLeaseRow",
    "TemplateBuildRow",
    "TrialRow",
]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    return json.loads(raw) if isinstance(raw, str) else raw


def _opt(raw: Any) -> str | None:
    return None if raw is None else str(raw)


@dataclass(frozen=True)
class ModelSpecRow:
    """One immutable model spec version (TECH-DESIGN 11 ``model_spec``)."""

    slug: str
    spec_version: int
    family: str
    roles: tuple[str, ...]
    source: dict[str, Any]
    deployment: dict[str, Any]
    declared_context_tokens: int
    declared_max_output_tokens: int
    pricing_ref: str
    license: str
    owner: str
    created_at: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.slug,
            self.spec_version,
            self.family,
            _json_dumps(list(self.roles)),
            _json_dumps(self.source),
            _json_dumps(self.deployment),
            self.declared_context_tokens,
            self.declared_max_output_tokens,
            self.pricing_ref,
            self.license,
            self.owner,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            slug=str(row["slug"]),
            spec_version=int(row["spec_version"]),
            family=str(row["family"]),
            roles=tuple(_json_loads(row["roles"], ())),
            source=dict(_json_loads(row["source"], {})),
            deployment=dict(_json_loads(row["deployment"], {})),
            declared_context_tokens=int(row["declared_context_tokens"]),
            declared_max_output_tokens=int(row["declared_max_output_tokens"]),
            pricing_ref=str(row["pricing_ref"]),
            license=str(row["license"]),
            owner=str(row["owner"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class HfArtifactRow:
    """Frozen HF mirror and its verification record (``hf_artifact``)."""

    repo: str
    revision: str
    mirror_repo: str | None
    files_sha256: dict[str, str]
    safetensors_bytes: int
    license: str
    verified_at: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.repo,
            self.revision,
            self.mirror_repo,
            _json_dumps(self.files_sha256),
            self.safetensors_bytes,
            self.license,
            self.verified_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            repo=str(row["repo"]),
            revision=str(row["revision"]),
            mirror_repo=_opt(row["mirror_repo"]),
            files_sha256=dict(_json_loads(row["files_sha256"], {})),
            safetensors_bytes=int(row["safetensors_bytes"]),
            license=str(row["license"]),
            verified_at=str(row["verified_at"]),
        )


@dataclass(frozen=True)
class EndpointRow:
    """Endpoint lifecycle projection (``endpoint``); the event log is authoritative."""

    endpoint_id: str
    model_slug: str
    model_spec_version: int
    adapter: str
    account: str
    state: str
    external_ids: dict[str, str]
    observed: dict[str, Any]
    created_at: str
    retired_at: str | None = None

    def params(self) -> tuple[Any, ...]:
        return (
            self.endpoint_id,
            self.model_slug,
            self.model_spec_version,
            self.adapter,
            self.account,
            self.state,
            _json_dumps(self.external_ids),
            _json_dumps(self.observed),
            self.created_at,
            self.retired_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            endpoint_id=str(row["endpoint_id"]),
            model_slug=str(row["model_slug"]),
            model_spec_version=int(row["model_spec_version"]),
            adapter=str(row["adapter"]),
            account=str(row["account"]),
            state=str(row["state"]),
            external_ids=dict(_json_loads(row["external_ids"], {})),
            observed=dict(_json_loads(row["observed"], {})),
            created_at=str(row["created_at"]),
            retired_at=_opt(row["retired_at"]),
        )


@dataclass(frozen=True)
class EndpointTicketRow:
    """Assisted-mode operator ticket (``endpoint_ticket``)."""

    ticket_id: str
    endpoint_id: str
    kind: str
    instructions_md: str
    status: str
    operator: str | None = None
    opened_at: str = ""
    closed_at: str | None = None

    def params(self) -> tuple[Any, ...]:
        return (
            self.ticket_id,
            self.endpoint_id,
            self.kind,
            self.instructions_md,
            self.status,
            self.operator,
            self.opened_at,
            self.closed_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            ticket_id=str(row["ticket_id"]),
            endpoint_id=str(row["endpoint_id"]),
            kind=str(row["kind"]),
            instructions_md=str(row["instructions_md"]),
            status=str(row["status"]),
            operator=_opt(row["operator"]),
            opened_at=str(row["opened_at"]),
            closed_at=_opt(row["closed_at"]),
        )


@dataclass(frozen=True)
class ProbeReportRow:
    """Immutable capability probe report (``probe_report``)."""

    report_id: str
    endpoint_id: str
    gateway_version: str
    results: dict[str, Any]
    capabilities: dict[str, Any]
    created_at: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.report_id,
            self.endpoint_id,
            self.gateway_version,
            _json_dumps(self.results),
            _json_dumps(self.capabilities),
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            report_id=str(row["report_id"]),
            endpoint_id=str(row["endpoint_id"]),
            gateway_version=str(row["gateway_version"]),
            results=dict(_json_loads(row["results"], {})),
            capabilities=dict(_json_loads(row["capabilities"], {})),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class BenchmarkRow:
    """One immutable inference benchmark of an endpoint (``endpoint_benchmark``)."""

    bench_id: str
    endpoint_id: str
    created_at: str
    plan: dict[str, Any]
    facts: dict[str, Any]
    """Hardware, engine and model facts of the endpoint when it was measured."""
    result: dict[str, Any]
    """The computed result document, a pure function of the raw samples."""
    raw_path: str | None
    raw_sha256: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.bench_id,
            self.endpoint_id,
            self.created_at,
            _json_dumps(self.plan),
            _json_dumps(self.facts),
            _json_dumps(self.result),
            self.raw_path,
            self.raw_sha256,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            bench_id=str(row["bench_id"]),
            endpoint_id=str(row["endpoint_id"]),
            created_at=str(row["created_at"]),
            plan=dict(_json_loads(row["plan"], {})),
            facts=dict(_json_loads(row["facts"], {})),
            result=dict(_json_loads(row["result"], {})),
            raw_path=_opt(row["raw_path"]),
            raw_sha256=str(row["raw_sha256"]),
        )


@dataclass(frozen=True)
class ProviderInstanceRow:
    """Capability declaration and archived isolation profile (``provider_instance``)."""

    instance: str
    adapter: str
    version: str
    capabilities: dict[str, Any]
    isolation_profile: str
    verified_isolation: bool
    config_hash: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.instance,
            self.adapter,
            self.version,
            _json_dumps(self.capabilities),
            self.isolation_profile,
            1 if self.verified_isolation else 0,
            self.config_hash,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            instance=str(row["instance"]),
            adapter=str(row["adapter"]),
            version=str(row["version"]),
            capabilities=dict(_json_loads(row["capabilities"], {})),
            isolation_profile=str(row["isolation_profile"]),
            verified_isolation=bool(row["verified_isolation"]),
            config_hash=str(row["config_hash"]),
        )


@dataclass(frozen=True)
class HostRow:
    """Local host inventory entry (``host``)."""

    host_id: str
    instance: str
    os: str
    hw: str
    hostd_version: str
    state: str
    last_heartbeat: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.host_id,
            self.instance,
            self.os,
            self.hw,
            self.hostd_version,
            self.state,
            self.last_heartbeat,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            host_id=str(row["host_id"]),
            instance=str(row["instance"]),
            os=str(row["os"]),
            hw=str(row["hw"]),
            hostd_version=str(row["hostd_version"]),
            state=str(row["state"]),
            last_heartbeat=str(row["last_heartbeat"]),
        )


@dataclass(frozen=True)
class TemplateBuildRow:
    """Template registry entry (``template_build``)."""

    env_id: str
    env_version: str
    oci_digest: str
    provider_template_ids: dict[str, str]
    fingerprints: dict[str, str]
    status: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.env_id,
            self.env_version,
            self.oci_digest,
            _json_dumps(self.provider_template_ids),
            _json_dumps(self.fingerprints),
            self.status,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            env_id=str(row["env_id"]),
            env_version=str(row["env_version"]),
            oci_digest=str(row["oci_digest"]),
            provider_template_ids=dict(_json_loads(row["provider_template_ids"], {})),
            fingerprints=dict(_json_loads(row["fingerprints"], {})),
            status=str(row["status"]),
        )


@dataclass(frozen=True)
class SandboxLeaseRow:
    """One sandbox lease; a trial holds at least an agent and a verifier lease."""

    lease_id: str
    batch_id: str
    trial_id: str
    role: str
    template_kind: str
    template_ref: str
    template_digest: str
    created_at: str
    provider_instance: str | None = None
    host_id: str | None = None
    destroyed_at: str | None = None
    reaped: bool = False

    def params(self) -> tuple[Any, ...]:
        return (
            self.lease_id,
            self.batch_id,
            self.trial_id,
            self.role,
            self.provider_instance,
            self.host_id,
            self.template_kind,
            self.template_ref,
            self.template_digest,
            self.created_at,
            self.destroyed_at,
            1 if self.reaped else 0,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            lease_id=str(row["lease_id"]),
            batch_id=str(row["batch_id"]),
            trial_id=str(row["trial_id"]),
            role=str(row["role"]),
            provider_instance=_opt(row["provider_instance"]),
            host_id=_opt(row["host_id"]),
            template_kind=str(row["template_kind"]),
            template_ref=str(row["template_ref"]),
            template_digest=str(row["template_digest"]),
            created_at=str(row["created_at"]),
            destroyed_at=_opt(row["destroyed_at"]),
            reaped=bool(row["reaped"]),
        )


@dataclass(frozen=True)
class GatewayKeyRow:
    """Per-trial gateway key metadata; the key material itself is never stored."""

    key_id: str
    trial_id: str
    endpoint_id: str
    budget: dict[str, Any]
    expires_at: str
    revoked_at: str | None = None

    def params(self) -> tuple[Any, ...]:
        return (
            self.key_id,
            self.trial_id,
            self.endpoint_id,
            _json_dumps(self.budget),
            self.expires_at,
            self.revoked_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            key_id=str(row["key_id"]),
            trial_id=str(row["trial_id"]),
            endpoint_id=str(row["endpoint_id"]),
            budget=dict(_json_loads(row["budget"], {})),
            expires_at=str(row["expires_at"]),
            revoked_at=_opt(row["revoked_at"]),
        )


@dataclass(frozen=True)
class TrialRow:
    """Trial projection row (v2 ``trial`` enriched with v3 version fields via extensions)."""

    trial_id: str
    batch_id: str
    task_id: str
    model: str
    shell: str
    state: str
    validity: str
    created_at: str
    updated_at: str
    cell: str | None = None
    epoch: int = 1
    idx: int = 0
    extensions: dict[str, Any] = field(default_factory=dict)

    def params(self) -> tuple[Any, ...]:
        return (
            self.trial_id,
            self.batch_id,
            self.cell,
            self.task_id,
            self.model,
            self.shell,
            self.state,
            self.validity,
            self.epoch,
            self.idx,
            _json_dumps(self.extensions),
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            trial_id=str(row["trial_id"]),
            batch_id=str(row["batch_id"]),
            cell=_opt(row["cell"]),
            task_id=str(row["task_id"]),
            model=str(row["model"]),
            shell=str(row["shell"]),
            state=str(row["state"]),
            validity=str(row["validity"]),
            epoch=int(row["epoch"]),
            idx=int(row["idx"]),
            extensions=dict(_json_loads(row["extensions"], {})),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class GradeRow:
    """One grade version for a trial; regrading appends, never overwrites."""

    trial_id: str
    version: int
    tier: str
    status: str
    decided_by: str
    payload: dict[str, Any]
    created_at: str
    rule_id: str | None = None

    def params(self) -> tuple[Any, ...]:
        return (
            self.trial_id,
            self.version,
            self.tier,
            self.status,
            self.decided_by,
            self.rule_id,
            _json_dumps(self.payload),
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            trial_id=str(row["trial_id"]),
            version=int(row["version"]),
            tier=str(row["tier"]),
            status=str(row["status"]),
            decided_by=str(row["decided_by"]),
            payload=dict(_json_loads(row["payload"], {})),
            created_at=str(row["created_at"]),
            rule_id=_opt(row["rule_id"]),
        )


@dataclass(frozen=True)
class RoundRow:
    """One agent round of a trial."""

    trial_id: str
    round_no: int
    started_at: str
    payload: dict[str, Any]
    stopped_at: str | None = None
    agent_steps: int = 0

    def params(self) -> tuple[Any, ...]:
        return (
            self.trial_id,
            self.round_no,
            self.started_at,
            self.stopped_at,
            self.agent_steps,
            _json_dumps(self.payload),
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            trial_id=str(row["trial_id"]),
            round_no=int(row["round_no"]),
            started_at=str(row["started_at"]),
            payload=dict(_json_loads(row["payload"], {})),
            stopped_at=_opt(row["stopped_at"]),
            agent_steps=int(row["agent_steps"]),
        )


@dataclass(frozen=True)
class ArtifactRow:
    """Indexed trial artifact (path, digest, size)."""

    artifact_id: str
    trial_id: str
    kind: str
    path: str
    sha256: str
    size_bytes: int
    created_at: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.artifact_id,
            self.trial_id,
            self.kind,
            self.path,
            self.sha256,
            self.size_bytes,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            artifact_id=str(row["artifact_id"]),
            trial_id=str(row["trial_id"]),
            kind=str(row["kind"]),
            path=str(row["path"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class IncidentRow:
    """An opened (and maybe closed) incident tied to an error class."""

    incident_id: str
    error_class: str
    disposition: str
    detail: dict[str, Any]
    opened_at: str
    trial_id: str | None = None
    closed_at: str | None = None

    def params(self) -> tuple[Any, ...]:
        return (
            self.incident_id,
            self.trial_id,
            self.error_class,
            self.disposition,
            _json_dumps(self.detail),
            self.opened_at,
            self.closed_at,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            incident_id=str(row["incident_id"]),
            error_class=str(row["error_class"]),
            disposition=str(row["disposition"]),
            detail=dict(_json_loads(row["detail"], {})),
            opened_at=str(row["opened_at"]),
            trial_id=_opt(row["trial_id"]),
            closed_at=_opt(row["closed_at"]),
        )


@dataclass(frozen=True)
class CanaryRow:
    """A canary token injected into a trial and its scan result."""

    canary_id: str
    trial_id: str
    token: str
    injected_at: str
    scanned_at: str | None = None
    hits: int = 0

    def params(self) -> tuple[Any, ...]:
        return (
            self.canary_id,
            self.trial_id,
            self.token,
            self.injected_at,
            self.scanned_at,
            self.hits,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            canary_id=str(row["canary_id"]),
            trial_id=str(row["trial_id"]),
            token=str(row["token"]),
            injected_at=str(row["injected_at"]),
            scanned_at=_opt(row["scanned_at"]),
            hits=int(row["hits"]),
        )
