"""Freeze record: everything FR-FLOW-01 freezes, hashed into the version matrix hash.

Any change to a frozen input needs a new batch, so the hash is a SHA-256 over the canonical JSON
(sorted keys, no whitespace, UTF-8) of the version matrix. ``frozen_at`` and ``batch_id`` are
record metadata and stay out of the hash: the same inputs always hash the same.

``provider_environments`` (host, sandbox runtime and guest of every provider instance the batch
can use, ``SandboxProvider.environment()``) is part of the hash: a runtime upgrade or a
different machine is a different batch. It stays deterministic because providers report only
stable facts; volatile readings (memory in use, uptime, load, running containers, clocks) are
never collected in the first place, so nothing has to be excluded here. An empty mapping is
left out of the hashed matrix, so records frozen before environments were recorded keep the
hash they were stored with.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from evalspleef.spi.isolation import IsolationProfile

from .plan import BatchPlan

__all__ = ["FreezeRecord", "build_freeze_record", "profile_summary"]

_MATRIX_FIELDS = (
    "probe_report_ids",
    "model_revisions",
    "model_spec_versions",
    "template_digests",
    "gateway_version",
    "price_table_version",
    "runner_versions",
    "isolation_profiles",
    "task_versions",
    "judges",
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def profile_summary(profile: IsolationProfile) -> dict[str, Any]:
    """Canonical, JSON-ready form of an isolation profile for hashing and storage."""
    return {
        "provider": profile.provider,
        "declared_isolation": int(profile.declared_isolation),
        "verified_isolation": (
            None if profile.verified_isolation is None else int(profile.verified_isolation)
        ),
        "invariants": {key: asdict(entry) for key, entry in sorted(profile.invariants.items())},
    }


@dataclass(frozen=True)
class FreezeRecord:
    batch_id: str
    probe_report_ids: Mapping[str, str]
    """Endpoint id -> probe report id, for the target models and the judge panel."""
    model_revisions: Mapping[str, str | None]
    """Model slug -> frozen weights revision; ``None`` for hosted API models."""
    model_spec_versions: Mapping[str, int]
    template_digests: Mapping[str, Mapping[str, str]]
    """Template ref -> provider instance -> template digest."""
    gateway_version: str
    price_table_version: str
    runner_versions: Mapping[str, str]
    """Shell slug -> runner version."""
    isolation_profiles: Mapping[str, Mapping[str, Any]]
    """Provider instance -> :func:`profile_summary`."""
    task_versions: Mapping[str, str]
    judges: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Judge model slug -> spec version, endpoint id, family and weights revision."""
    frozen_at: str = ""
    provider_environments: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Provider instance -> environment record (``control.environment``)."""

    def _matrix_fields(self) -> tuple[str, ...]:
        if self.provider_environments:
            return (*_MATRIX_FIELDS, "provider_environments")
        return _MATRIX_FIELDS

    def matrix(self) -> dict[str, Any]:
        """The hashed part of the record as plain JSON types."""
        return {name: _plain(getattr(self, name)) for name in self._matrix_fields()}

    @property
    def version_matrix_hash(self) -> str:
        canonical = json.dumps(
            self.matrix(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def diff(self, other: FreezeRecord) -> tuple[str, ...]:
        """Names of the frozen inputs that differ from ``other``."""
        mine, theirs = self.matrix(), other.matrix()
        names = (*_MATRIX_FIELDS, "provider_environments")
        return tuple(name for name in names if mine.get(name) != theirs.get(name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            **self.matrix(),
            "version_matrix_hash": self.version_matrix_hash,
            "frozen_at": self.frozen_at,
        }


def build_freeze_record(
    plan: BatchPlan,
    *,
    gateway_version: str,
    price_table_version: str,
    runner_versions: Mapping[str, str],
    template_digests: Mapping[str, Mapping[str, str]],
    isolation_profiles: Mapping[str, IsolationProfile],
    judges: Mapping[str, Mapping[str, Any]] | None = None,
    judge_probe_reports: Mapping[str, str] | None = None,
    frozen_at: str = "",
    provider_environments: Mapping[str, Mapping[str, Any]] | None = None,
) -> FreezeRecord:
    """Freeze the inputs of the runnable cells of ``plan``.

    Only models, tasks, templates and provider instances that a runnable cell uses are part of
    the record, so unrelated changes do not force a new batch. ``judge_probe_reports`` (judge
    endpoint id -> probe report id) extends the version matrix to the judge panel; on the rare
    overlap of a judge and a target endpoint the target's frozen report id wins.
    """
    cells = plan.runnable_cells
    models = {cell.model.slug: cell.model for cell in cells}
    templates = {cell.task.template.ref for cell in cells}
    instances: list[str] = []
    for cell in cells:
        for instance in (*cell.match.agent.candidates, *cell.match.verifier.candidates):
            if instance not in instances:
                instances.append(instance)
    shells = {cell.model.shell for cell in cells}
    return FreezeRecord(
        batch_id=plan.batch_id,
        probe_report_ids={
            **(judge_probe_reports or {}),
            **{
                cell.model.endpoint_id: cell.model.probe_report_id
                for cell in cells
                if cell.model.probe_report_id is not None
            },
        },
        model_revisions={slug: model.revision for slug, model in models.items()},
        model_spec_versions={slug: model.spec_version for slug, model in models.items()},
        template_digests={
            ref: dict(mapping) for ref, mapping in template_digests.items() if ref in templates
        },
        gateway_version=gateway_version,
        price_table_version=price_table_version,
        runner_versions={shell: v for shell, v in runner_versions.items() if shell in shells},
        isolation_profiles={
            instance: profile_summary(isolation_profiles[instance])
            for instance in instances
            if instance in isolation_profiles
        },
        task_versions={cell.task.task_id: cell.task.version for cell in cells},
        judges={slug: dict(judge) for slug, judge in (judges or {}).items()},
        frozen_at=frozen_at,
        provider_environments={
            instance: dict(record)
            for instance, record in (provider_environments or {}).items()
            if instance in instances
        },
    )
