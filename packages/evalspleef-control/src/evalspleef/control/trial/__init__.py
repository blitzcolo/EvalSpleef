"""Trial runner: one trial from lease to grade (PRD appendix B.2-B.4, TECH-DESIGN section 10)."""

from .errors import (
    CanaryInjectionError,
    EvaluationTimeout,
    IdentityHygieneError,
    IsolationPreflightFailed,
    RunnerCrashed,
)
from .feedback_text import DefectMaterial, PackFeedbackWriter
from .protocols import (
    AgentSandboxReplacer,
    Clock,
    Evaluator,
    FeedbackWriter,
    ProviderDirectory,
    RunnerFactory,
    TrialSetupResolver,
)
from .runner import ErrorVerdict, TrialRunner, classify_error
from .types import (
    Artifact,
    CheatSignal,
    EvaluationResult,
    ModelEndpoint,
    RubricItemResult,
    RubricVerdict,
    TrialContext,
    TrialSetup,
    WorkspacePaths,
)

__all__ = [
    "AgentSandboxReplacer",
    "Artifact",
    "CanaryInjectionError",
    "CheatSignal",
    "Clock",
    "DefectMaterial",
    "ErrorVerdict",
    "EvaluationResult",
    "EvaluationTimeout",
    "Evaluator",
    "FeedbackWriter",
    "IdentityHygieneError",
    "IsolationPreflightFailed",
    "ModelEndpoint",
    "PackFeedbackWriter",
    "ProviderDirectory",
    "RubricItemResult",
    "RubricVerdict",
    "RunnerCrashed",
    "RunnerFactory",
    "TrialContext",
    "TrialRunner",
    "TrialSetup",
    "TrialSetupResolver",
    "WorkspacePaths",
    "classify_error",
]
