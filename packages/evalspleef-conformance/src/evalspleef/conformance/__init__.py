"""Conformance kit: pytest plugin, case contract, targets, report and builtin targets.

Imports are lazy (PEP 562) so that loading the ``pytest11`` entry point does not
pull in the test-double stack unless conformance code is actually used.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evalspleef.conformance.case import Case
    from evalspleef.conformance.report import CaseResult, ConformanceReport, TargetSummary
    from evalspleef.conformance.target import ConformanceTarget, Injector, ModelFixture

__all__ = [
    "Case",
    "CaseResult",
    "ConformanceReport",
    "ConformanceTarget",
    "Injector",
    "ModelFixture",
    "TargetSummary",
]


def __getattr__(name: str) -> Any:
    mapping = {
        "Case": ("case", "Case"),
        "ConformanceTarget": ("target", "ConformanceTarget"),
        "Injector": ("target", "Injector"),
        "ModelFixture": ("target", "ModelFixture"),
        "CaseResult": ("report", "CaseResult"),
        "ConformanceReport": ("report", "ConformanceReport"),
        "TargetSummary": ("report", "TargetSummary"),
    }
    if name in mapping:
        import importlib

        module, attribute = mapping[name]
        value = getattr(importlib.import_module(f"evalspleef.conformance.{module}"), attribute)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
