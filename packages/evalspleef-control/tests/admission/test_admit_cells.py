"""Cell admission: probe results x task requirements."""

from __future__ import annotations

from evalspleef.control.admission import CellOutcome, admit_cells
from evalspleef.control.probes import build_report
from evalspleef.spi.caps import ModelRequirements
from evalspleef.spi.probe import ProbeCheck, ProbeReport

NEEDS_TOOLS = ModelRequirements(tool_calling="required")
NEEDS_CONTEXT = ModelRequirements(min_context_tokens=50_000)
NEEDS_BOTH = ModelRequirements(tool_calling="required", min_context_tokens=50_000)
NO_REQUIREMENTS = ModelRequirements()


def report(*checks: ProbeCheck) -> ProbeReport:
    return build_report("ep_x", "gw-1", "2026-11-07T00:00:00Z", {c.probe_id: c for c in checks})


def tools(*, works: bool) -> ProbeCheck:
    return ProbeCheck("P-03", works, data={"valid_rate": 1.0 if works else 0.0})


def context(tokens: int) -> ProbeCheck:
    return ProbeCheck("P-09", tokens > 0, data={"effective": tokens})


def skipped(probe_id: str) -> ProbeCheck:
    return ProbeCheck(probe_id, False, "skipped: P-01 failed", {"skipped": True})


def errored(probe_id: str) -> ProbeCheck:
    return ProbeCheck(probe_id, False, "ReadTimeout", {"error": True})


def decide(model_report: ProbeReport | None, req: ModelRequirements) -> tuple[str, tuple[str, ...]]:
    (decision,) = admit_cells({"m": model_report}, {"t": req}).decisions
    return decision.outcome.value, decision.reasons


def test_capable_model_runs_the_cell() -> None:
    capable = report(tools(works=True), context(100_000))

    assert decide(capable, NEEDS_BOTH) == ("run", ())


def test_shortfalls_skip_the_cell_with_capability_reasons() -> None:
    assert decide(report(tools(works=False), context(100_000)), NEEDS_TOOLS) == (
        "skip",
        ("capability:tool_calling",),
    )
    assert decide(report(tools(works=True), context(8_000)), NEEDS_CONTEXT) == (
        "skip",
        ("capability:context_tokens_effective",),
    )
    outcome, reasons = decide(report(tools(works=False), context(8_000)), NEEDS_BOTH)
    assert outcome == "skip"
    assert set(reasons) == {"capability:tool_calling", "capability:context_tokens_effective"}


def test_a_capability_no_probe_could_determine_makes_the_cell_manual() -> None:
    # Skipped after a failed chat probe, or lost to a transport error: never guessed as "no".
    assert decide(report(skipped("P-03"), context(100_000)), NEEDS_TOOLS) == (
        "manual",
        ("undetermined:tool_calling",),
    )
    assert decide(report(tools(works=True), errored("P-09")), NEEDS_CONTEXT) == (
        "manual",
        ("undetermined:context_tokens_effective",),
    )
    assert decide(report(tools(works=True)), NEEDS_CONTEXT) == (
        "manual",
        ("undetermined:context_tokens_effective",),
    )


def test_a_proven_shortfall_wins_over_an_undetermined_capability() -> None:
    assert decide(report(tools(works=False), errored("P-09")), NEEDS_BOTH) == (
        "skip",
        ("capability:tool_calling",),
    )


def test_a_model_without_a_report_is_manual_only_where_a_requirement_exists() -> None:
    assert decide(None, NEEDS_BOTH) == (
        "manual",
        ("undetermined:tool_calling", "undetermined:context_tokens_effective"),
    )
    assert decide(None, NO_REQUIREMENTS) == ("run", ())
    assert decide(report(skipped("P-03"), skipped("P-09")), NO_REQUIREMENTS) == ("run", ())


def test_matrix_covers_every_model_and_task_in_input_order() -> None:
    models = {
        "strong": report(tools(works=True), context(100_000)),
        "weak": report(tools(works=False), context(8_000)),
        "unprobed": None,
    }
    tasks = {"plain": NO_REQUIREMENTS, "agentic": NEEDS_TOOLS, "long": NEEDS_CONTEXT}

    result = admit_cells(models, tasks)

    assert [(d.model, d.task) for d in result.decisions] == [
        (model, task) for model in models for task in tasks
    ]
    assert {(d.model, d.task) for d in result.runnable} == {
        ("strong", "plain"),
        ("strong", "agentic"),
        ("strong", "long"),
        ("weak", "plain"),
        ("unprobed", "plain"),
    }
    assert {(d.model, d.task) for d in result.skipped} == {("weak", "agentic"), ("weak", "long")}
    assert {(d.model, d.task) for d in result.manual} == {
        ("unprobed", "agentic"),
        ("unprobed", "long"),
    }
    assert all(d.outcome is CellOutcome.SKIP for d in result.skipped)
