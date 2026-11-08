"""Cost basis, price math, GPU allocation and report prov(cost) integration."""

from __future__ import annotations

import math

import pytest

from evalspleef.core.cost import (
    CostBasis,
    CostBreakdown,
    ModelPrice,
    PriceTable,
    allocate_gpu_cost,
    price_usage,
)
from evalspleef.core.models import CellStatus, Tier
from evalspleef.core.report import BatchReport, Cell, TrialResult, aggregate_cell, format_cell

TABLE = PriceTable(
    version="2026-11-official",
    prices={
        "claude-sonnet-5": ModelPrice(
            input_per_mtok=3.0,
            output_per_mtok=15.0,
            cached_per_mtok=0.3,
            basis=CostBasis.API,
        ),
        "qwen3-32b": ModelPrice(
            input_per_mtok=0.5,
            output_per_mtok=1.5,
            cached_per_mtok=0.05,
            basis=CostBasis.REF_PRICE,
        ),
    },
)


def test_basis_spellings_are_frozen() -> None:
    assert [str(basis) for basis in CostBasis] == ["api", "ref-price", "assumed"]


def test_worked_example_price_math() -> None:
    breakdown = price_usage(
        TABLE,
        "qwen3-32b",
        input_tokens=1_000_000,
        output_tokens=200_000,
        cached_tokens=400_000,
    )
    assert isinstance(breakdown, CostBreakdown)
    assert breakdown.input_cost_usd == pytest.approx(0.5)
    assert breakdown.output_cost_usd == pytest.approx(0.3)
    assert breakdown.cached_cost_usd == pytest.approx(0.02)
    assert breakdown.total_usd == pytest.approx(0.82)
    assert breakdown.price_table_version == "2026-11-official"
    assert breakdown.basis == CostBasis.REF_PRICE
    assert breakdown.to_dict()["total_usd"] == pytest.approx(0.82)


def test_api_basis_and_fractional_tokens() -> None:
    breakdown = price_usage(
        TABLE, "claude-sonnet-5", input_tokens=1_000, output_tokens=500, cached_tokens=0
    )
    assert breakdown.total_usd == pytest.approx(0.003 + 0.0075)
    assert breakdown.basis == CostBasis.API


def test_unknown_model_and_bad_usage_are_rejected() -> None:
    with pytest.raises(KeyError, match="not in price table"):
        price_usage(TABLE, "gpt-5-codex", input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError, match="input_tokens"):
        price_usage(TABLE, "qwen3-32b", input_tokens=-1, output_tokens=0)
    with pytest.raises(TypeError):
        price_usage(TABLE, "qwen3-32b", input_tokens=1.5, output_tokens=0)  # type: ignore[arg-type]


def test_gpu_allocation_splits_by_token_share() -> None:
    allocation = allocate_gpu_cost(12.0, {"t1": 600, "t2": 400})
    assert allocation["t1"] == pytest.approx(7.2)
    assert allocation["t2"] == pytest.approx(4.8)
    assert sum(allocation.values()) == pytest.approx(12.0)


def test_gpu_allocation_sums_to_batch_cost_with_uneven_shares() -> None:
    allocation = allocate_gpu_cost(100.0, {"a": 1, "b": 2, "c": 7, "d": 999999})
    assert math.fsum(allocation.values()) == pytest.approx(100.0)
    assert allocation["a"] == pytest.approx(100.0 / 1_000_009)
    assert allocation["c"] == pytest.approx(700.0 / 1_000_009)


def test_gpu_allocation_rejects_undefined_shares() -> None:
    with pytest.raises(ValueError, match="at least one trial"):
        allocate_gpu_cost(1.0, {})
    with pytest.raises(ValueError, match="positive token count"):
        allocate_gpu_cost(1.0, {"t1": 0, "t2": 0})
    with pytest.raises(ValueError, match="non-negative"):
        allocate_gpu_cost(-1.0, {"t1": 1})
    with pytest.raises(ValueError, match="negative"):
        allocate_gpu_cost(1.0, {"t1": -1, "t2": 2})


def make_trial(trial_id: str, tier: str) -> TrialResult:
    return TrialResult.from_record(
        {
            "trial_id": trial_id,
            "state": "scored",
            "validity": "valid",
            "cost_basis": "ref-price",
            "provenance": {
                "model_source": "huggingface",
                "deployment_adapter": "novita-dedicated",
                "sandbox_adapter": "docker",
            },
            "grades": [
                {
                    "tier": tier,
                    "penalty": 6,
                    "cost": {
                        "agent": 290.08,
                        "judge": 1.0,
                        "user": 0.1,
                        "total": 291.18,
                        "currency": "USD",
                    },
                    "status": "final",
                    "counted_in_leaderboard": True,
                    "flags": [],
                }
            ],
        }
    )


def test_trial_result_carries_basis_and_provenance() -> None:
    trial = make_trial("web-001.m.claude-code.e1.t01", "A")
    assert trial.cost_basis == CostBasis.REF_PRICE
    payload = trial.to_dict()
    assert payload["cost_basis"] == "ref-price"
    assert payload["provenance"]["deployment_adapter"] == "novita-dedicated"


def test_cost_basis_can_come_from_grade_cost() -> None:
    trial = TrialResult.from_record(
        {
            "trial_id": "web-001.m.claude-code.e1.t01",
            "state": "scored",
            "validity": "valid",
            "grades": [
                {
                    "tier": "A",
                    "penalty": 1,
                    "cost": {"agent": 1.0, "basis": "assumed"},
                    "status": "final",
                    "counted_in_leaderboard": True,
                    "flags": [],
                }
            ],
        }
    )
    assert trial.cost_basis == CostBasis.ASSUMED


def test_cell_format_contract_is_unchanged() -> None:
    cell = Cell(
        task_id="web-001",
        model="claude-sonnet-5",
        shell="claude-code",
        status=CellStatus.SCORED,
        tier=Tier.A,
        penalty=6,
        cost_agent=290.08,
        cost_basis=CostBasis.REF_PRICE,
        provenance={"deployment_adapter": "novita-dedicated"},
    )
    assert format_cell(cell) == "6/A(290.08)"
    payload = cell.to_dict()
    assert payload["cell"] == "6/A(290.08)"
    assert payload["cost_basis"] == "ref-price"
    assert payload["provenance"] == {"deployment_adapter": "novita-dedicated"}


def test_aggregate_cell_propagates_unanimous_basis_and_provenance() -> None:
    trials = [
        make_trial("web-001.m.claude-code.e1.t01", "A"),
        make_trial("web-001.m.claude-code.e1.t02", "A"),
    ]
    cell = aggregate_cell(trials)
    assert cell.status == CellStatus.SCORED
    assert cell.cost_basis == CostBasis.REF_PRICE
    assert cell.provenance == trials[0].provenance
    assert cell.format() == "6/A(290.08)"


def test_aggregate_cell_drops_conflicting_basis() -> None:
    first = make_trial("web-001.m.claude-code.e1.t01", "A")
    second = TrialResult(
        trial_id="web-001.m.claude-code.e1.t02",
        task_id="web-001",
        model="m",
        shell="claude-code",
        epoch=1,
        index=2,
        validity=first.validity,
        state=first.state,
        tier=Tier.A,
        penalty=6,
        cost_agent=290.08,
        counted_in_leaderboard=True,
        cost_basis=CostBasis.API,
        provenance={"model_source": "vendor"},
    )
    cell = aggregate_cell([first, second])
    assert cell.cost_basis is None
    assert cell.provenance is None


def _scored(trial_id: str, basis: str) -> TrialResult:
    """A scored trial whose basis travels in ``extensions``, the way the runner records it."""
    return TrialResult.from_record(
        {
            "trial_id": trial_id,
            "state": "scored",
            "validity": "valid",
            "extensions": {"cost_basis": basis},
            "grades": [
                {
                    "tier": "A",
                    "penalty": 1,
                    "cost": {"agent": 1.0},
                    "status": "final",
                    "counted_in_leaderboard": True,
                    "flags": [],
                }
            ],
        }
    )


def test_report_labels_each_column_with_its_cost_basis() -> None:
    cells = [
        ("web-001", "api-model", "api"),
        ("web-001", "self-hosted", "ref-price"),
        ("web-002", "self-hosted", "ref-price"),
        ("web-001", "mixed", "api"),
        ("web-002", "mixed", "assumed"),
    ]
    report = BatchReport(
        batch_id="2026-11-b1",
        trials=tuple(
            _scored(f"{task}.{model}.terminus.e{epoch}.t01", basis)
            for task, model, basis in cells
            for epoch in (1, 2)
        ),
    )
    assert report.cost_bases() == {"api-model": "api", "mixed": None, "self-hosted": "ref-price"}
    assert report.to_dict()["cost_bases"] == report.cost_bases()
