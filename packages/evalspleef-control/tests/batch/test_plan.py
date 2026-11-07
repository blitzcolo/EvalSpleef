"""Batch planner: cell matrix, skip reasons and the budget plan."""

from __future__ import annotations

from evalspleef.spi.caps import ModelRequirements, OSFamily
from evalspleef.spi.probe import ModelCapabilities


async def test_cells_cover_models_tasks_and_epochs(build) -> None:
    h = await build(n_models=3, n_tasks=2, epochs=2)

    assert len(h.plan.cells) == 6
    assert len(h.plan.trials) == 12
    first = h.plan.cells[0]
    assert first.cell_id == "web-001.alpha.terminus"
    assert [t.trial_id for t in first.trials] == [
        "web-001.alpha.terminus.e1.t01",
        "web-001.alpha.terminus.e2.t01",
    ]
    trial = first.trials[0]
    assert trial.agent_routes == ("docker-a",)
    assert trial.verifier_routes == ("docker-a",)
    assert trial.endpoint_id == "ep_alpha_00000000"
    assert (trial.epoch, trial.index, trial.attempt) == (1, 1, 0)


async def test_cells_no_provider_can_host_are_skipped_with_the_reason_list(
    build, task_factory
) -> None:
    h = await build(
        tasks=[
            task_factory("web-001"),
            task_factory("web-002", os=OSFamily.WINDOWS),
            task_factory("web-003", gpu=True),
        ]
    )

    cells = {cell.cell_id: cell for cell in h.plan.cells}
    assert not cells["web-001.alpha.terminus"].skipped
    assert cells["web-002.alpha.terminus"].skip_reasons == ("capability:route:none",)
    assert cells["web-003.alpha.terminus"].skip_reason == "capability:gpu"
    assert cells["web-002.alpha.terminus"].trials == ()
    assert h.plan.skips() == {
        "web-002.alpha.terminus": "capability:route:none",
        "web-003.alpha.terminus": "capability:gpu",
    }
    assert [t.cell_id for t in h.plan.trials] == ["web-001.alpha.terminus"]


async def test_model_without_the_required_capabilities_is_skipped(
    build, task_factory, model_factory
) -> None:
    weak = ModelCapabilities(tool_calling="none", context_tokens_effective=8000)
    strong = ModelCapabilities(tool_calling="parallel", context_tokens_effective=64000)
    h = await build(
        tasks=[
            task_factory(
                "web-001",
                model=ModelRequirements(tool_calling="required", min_context_tokens=32000),
            )
        ],
        models=[
            model_factory("alpha", capabilities=weak),
            model_factory("beta", capabilities=strong),
        ],
    )

    cells = {cell.model.slug: cell for cell in h.plan.cells}
    assert cells["alpha"].skip_reasons == (
        "capability:tool_calling",
        "capability:context_tokens_effective",
    )
    assert not cells["beta"].skipped


async def test_budget_plan_is_worst_case_agent_spend_of_runnable_cells(build, task_factory) -> None:
    capped = await build(n_models=3, n_tasks=2, epochs=2, budget_cap=100.0)
    roomy = await build(n_models=3, n_tasks=2, epochs=2, budget_cap=200.0)
    open_ended = await build(n_models=3, n_tasks=2, epochs=2)
    with_skip = await build(tasks=[task_factory("web-001"), task_factory("web-002", gpu=True)])

    assert set(capped.plan.budget.per_cell_usd.values()) == {30.0}  # 15 USD cap x 2 epochs
    assert capped.plan.budget.total_usd == 180.0
    assert not capped.plan.budget.within_cap
    assert roomy.plan.budget.within_cap
    assert open_ended.plan.budget.within_cap
    assert with_skip.plan.budget.per_cell_usd == {"web-001.alpha.terminus": 15.0}
