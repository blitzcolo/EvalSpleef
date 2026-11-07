"""``TemplateRegistry`` and the cross-provider fingerprint comparison."""

from __future__ import annotations

import builtins
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from evalspleef.control.store.repos import TemplateBuildRepository
from evalspleef.control.store.rows import TemplateBuildRow
from evalspleef.spi.errors import SpleefError

__all__ = [
    "BASELINE_KEY",
    "STATUS_PENDING",
    "STATUS_READY",
    "STATUS_REJECTED",
    "FingerprintComparison",
    "ProviderFingerprintDiff",
    "TemplateBuild",
    "TemplateError",
    "TemplateRegistry",
    "compare_fingerprints",
]

#: Fingerprint key of the OCI image every other carrier is compared against.
BASELINE_KEY = "oci"

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_REJECTED = "rejected"

#: Component name used when a fingerprint is an opaque string, not a JSON object.
WHOLE_FINGERPRINT = "fingerprint"


class TemplateError(SpleefError):
    """A template registration or comparison request is malformed."""

    error_class = "environment"
    disposition = "manual"


@dataclass(frozen=True)
class TemplateBuild:
    """One environment build: the OCI digest plus its per-carrier templates."""

    env_id: str
    env_version: str
    oci_digest: str
    provider_template_ids: Mapping[str, str] = field(default_factory=dict)
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    status: str = STATUS_PENDING


@dataclass(frozen=True)
class ProviderFingerprintDiff:
    """How one carrier's fingerprint deviates from the baseline.

    ``differing`` lists every deviating component; ``unexpected`` drops the ones
    the caller allowed (for example a vendor-injected daemon).
    """

    provider: str
    differing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.unexpected


@dataclass(frozen=True)
class FingerprintComparison:
    """Result of comparing carrier fingerprints with the OCI baseline."""

    baseline: str
    diffs: tuple[ProviderFingerprintDiff, ...]
    missing: tuple[str, ...]
    """Carriers that declare a template but have no fingerprint yet."""

    @property
    def ok(self) -> bool:
        return not self.missing and all(diff.ok for diff in self.diffs)


def _components(fingerprint: str) -> dict[str, Any]:
    """Split a fingerprint into named components.

    A fingerprint that is a JSON object (package-list hash, toolchain versions,
    users and permissions, PATH) compares per component; anything else is one
    opaque component.
    """
    try:
        document = json.loads(fingerprint)
    except ValueError:
        return {WHOLE_FINGERPRINT: fingerprint}
    if isinstance(document, dict):
        return {str(key): value for key, value in document.items()}
    return {WHOLE_FINGERPRINT: fingerprint}


def compare_fingerprints(
    fingerprints: Mapping[str, str],
    *,
    baseline: str = BASELINE_KEY,
    allowed_diffs: Mapping[str, Collection[str]] | None = None,
    expected_providers: Iterable[str] = (),
) -> FingerprintComparison:
    """Compare every carrier fingerprint with the baseline (TECH-DESIGN 7.6).

    ``allowed_diffs`` maps a carrier to the components that may deviate; any
    other deviation makes the comparison fail. Carriers in
    ``expected_providers`` without a fingerprint are reported as ``missing``.
    """
    if baseline not in fingerprints:
        raise TemplateError(f"no {baseline!r} baseline fingerprint to compare against")
    allowed = allowed_diffs or {}
    base = _components(fingerprints[baseline])
    diffs: list[ProviderFingerprintDiff] = []
    for provider in sorted(fingerprints):
        if provider == baseline:
            continue
        other = _components(fingerprints[provider])
        differing = tuple(
            sorted(key for key in base.keys() | other.keys() if base.get(key) != other.get(key))
        )
        tolerated = set(allowed.get(provider, ()))
        unexpected = tuple(key for key in differing if key not in tolerated)
        diffs.append(
            ProviderFingerprintDiff(provider=provider, differing=differing, unexpected=unexpected)
        )
    missing = tuple(
        sorted(p for p in expected_providers if p != baseline and p not in fingerprints)
    )
    return FingerprintComparison(baseline=baseline, diffs=tuple(diffs), missing=missing)


def _to_row(build: TemplateBuild) -> TemplateBuildRow:
    return TemplateBuildRow(
        env_id=build.env_id,
        env_version=build.env_version,
        oci_digest=build.oci_digest,
        provider_template_ids=dict(build.provider_template_ids),
        fingerprints=dict(build.fingerprints),
        status=build.status,
    )


def _from_row(row: TemplateBuildRow) -> TemplateBuild:
    return TemplateBuild(
        env_id=row.env_id,
        env_version=row.env_version,
        oci_digest=row.oci_digest,
        provider_template_ids=dict(row.provider_template_ids),
        fingerprints=dict(row.fingerprints),
        status=row.status,
    )


class TemplateRegistry:
    """``(env_id, env_version)`` to template mapping, persisted through the store."""

    def __init__(self, builds: TemplateBuildRepository) -> None:
        self._builds = builds

    async def register(self, build: TemplateBuild) -> None:
        """Insert or update one build; the OCI digest must be a pinned sha256."""
        if not build.env_id or not build.env_version:
            raise TemplateError("env_id and env_version must not be empty")
        if not build.oci_digest.startswith("sha256:"):
            raise TemplateError(
                f"oci_digest of {build.env_id}@{build.env_version} must be 'sha256:...', "
                f"got {build.oci_digest!r}"
            )
        await self._builds.upsert(_to_row(build))

    async def get(self, env_id: str, env_version: str) -> TemplateBuild | None:
        row = await self._builds.get(env_id, env_version)
        return _from_row(row) if row else None

    async def list(self, env_id: str) -> builtins.list[TemplateBuild]:
        """All registered versions of one environment, ordered by version."""
        return [_from_row(row) for row in await self._builds.list_by_env(env_id)]

    async def compare(
        self,
        env_id: str,
        env_version: str,
        *,
        allowed_diffs: Mapping[str, Collection[str]] | None = None,
    ) -> FingerprintComparison:
        """Compare a registered build's carrier fingerprints with its OCI baseline.

        Carriers that have a template id but no fingerprint are reported as missing.
        """
        build = await self.get(env_id, env_version)
        if build is None:
            raise TemplateError(f"no template build registered for {env_id}@{env_version}")
        return compare_fingerprints(
            build.fingerprints,
            allowed_diffs=allowed_diffs,
            expected_providers=build.provider_template_ids,
        )
