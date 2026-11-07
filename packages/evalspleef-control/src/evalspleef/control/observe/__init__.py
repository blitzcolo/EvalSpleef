"""Observe/verify/evaluate: the evaluator behind the trial runner.

Artifact export and sanitising (I11), the independent verifier sandbox with the pack's
hard gates and hash locks (I10/I12), check-result to rubric mapping, judge panel
wiring through the gateway (I15), and the :class:`Evaluator` implementation consumed
by the trial runner.
"""

from .artifacts import (
    ArtifactDecl,
    ExportCaps,
    SanitizeIssue,
    WorkspaceExport,
    artifact_decls,
    export_workspace,
    protected_paths,
    sanitise_tree,
)
from .evaluator import (
    ArtifactSource,
    Evaluator,
    SandboxSource,
    StoredArtifactSource,
)
from .judging import GatewayJudgeClient, JudgeRun, JudgeStep, evidence_digest
from .mapping import CatalogDetect, load_catalog_detects, map_script_items
from .verifier import (
    GateRun,
    HardGateDecl,
    HashLockEntry,
    HashLockMismatch,
    VerificationError,
    VerificationOutput,
    check_hash_locks,
    find_hash_manifest,
    load_hard_gates,
    parse_hash_manifest,
    requires_host_tools,
    run_verification,
)

__all__ = [
    "ArtifactDecl",
    "ArtifactSource",
    "CatalogDetect",
    "Evaluator",
    "ExportCaps",
    "GateRun",
    "GatewayJudgeClient",
    "HardGateDecl",
    "HashLockEntry",
    "HashLockMismatch",
    "JudgeRun",
    "JudgeStep",
    "SandboxSource",
    "SanitizeIssue",
    "StoredArtifactSource",
    "VerificationError",
    "VerificationOutput",
    "WorkspaceExport",
    "artifact_decls",
    "check_hash_locks",
    "evidence_digest",
    "export_workspace",
    "find_hash_manifest",
    "load_catalog_detects",
    "load_hard_gates",
    "map_script_items",
    "parse_hash_manifest",
    "protected_paths",
    "requires_host_tools",
    "run_verification",
    "sanitise_tree",
]
