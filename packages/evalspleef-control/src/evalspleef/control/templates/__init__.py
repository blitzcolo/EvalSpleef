"""Template registry (TECH-DESIGN section 7.6).

Maps ``(env_id, env_version)`` to the OCI digest, the per-provider template ids
and the ``env-fingerprint`` results, persisted through the store's
``template_build`` table. :func:`compare_fingerprints` implements the
cross-provider consistency check against the OCI baseline.
"""

from .registry import (
    BASELINE_KEY,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_REJECTED,
    FingerprintComparison,
    ProviderFingerprintDiff,
    TemplateBuild,
    TemplateError,
    TemplateRegistry,
    compare_fingerprints,
)

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
