"""Token pricing, cost basis and GPU cost allocation (PRD FR-MOD-08, TECH-DESIGN 6.7).

Two cost figures exist for every trial:

* the leaderboard figure, computed from token usage and a frozen price table
  (:func:`price_usage`), and
* for self-deployed models, the auxiliary GPU figure, obtained by splitting the
  batch's GPU cost across trials in proportion to their token usage
  (:func:`allocate_gpu_cost`).

Cost basis is part of the result, not a presentation detail: PRD FR-MOD-08
requires the leaderboard to mark every cell as ``api``, ``ref-price`` or
``assumed`` so figures with different bases are not compared silently.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "CostBasis",
    "CostBreakdown",
    "ModelPrice",
    "PriceTable",
    "allocate_gpu_cost",
    "price_usage",
]

#: Tokens in one million, the unit of every price in :class:`ModelPrice`.
_TOKENS_PER_MTOK = 1_000_000


class CostBasis(StrEnum):
    """How a cost figure was derived (PRD FR-MOD-08)."""

    API = "api"
    REF_PRICE = "ref-price"
    ASSUMED = "assumed"


@dataclass(frozen=True)
class ModelPrice:
    """Frozen USD prices per million tokens for one model."""

    input_per_mtok: float
    output_per_mtok: float
    cached_per_mtok: float
    basis: CostBasis = CostBasis.API

    def __post_init__(self) -> None:
        for name in ("input_per_mtok", "output_per_mtok", "cached_per_mtok"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative: {value!r}")
        if not isinstance(self.basis, CostBasis):
            raise ValueError(f"unknown cost basis: {self.basis!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "cached_per_mtok": self.cached_per_mtok,
            "basis": str(self.basis),
        }


@dataclass(frozen=True)
class PriceTable:
    """Versioned price table written into the batch record at freeze time.

    The version is part of the result: ``grade.cost.pricing_table_version``
    points at it, and prices are never refreshed mid-batch (PRD appendix B.5).
    """

    version: str
    prices: Mapping[str, ModelPrice] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("price table version must not be empty")

    def price_for(self, model: str) -> ModelPrice:
        """Return the price entry for ``model``.

        Raises:
            KeyError: The model is not in this price table version.
        """
        try:
            return self.prices[model]
        except KeyError as exc:
            raise KeyError(f"model {model!r} not in price table {self.version!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "prices": {model: price.to_dict() for model, price in sorted(self.prices.items())},
        }


@dataclass(frozen=True)
class CostBreakdown:
    """Token cost of one usage record, split by token class."""

    model: str
    price_table_version: str
    basis: CostBasis
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    cached_cost_usd: float

    @property
    def total_usd(self) -> float:
        """Sum of the three components in USD."""
        return self.input_cost_usd + self.output_cost_usd + self.cached_cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "price_table_version": self.price_table_version,
            "basis": str(self.basis),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "cached_cost_usd": self.cached_cost_usd,
            "total_usd": self.total_usd,
        }


def _tokens(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer: {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative: {value!r}")
    return value


def price_usage(
    table: PriceTable,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> CostBreakdown:
    """Price one usage record with the frozen table.

    Implements PRD appendix B.5 ``trial.cost.agent``:
    ``input_tokens x price_in + output_tokens x price_out + cache_read x price_cache``.

    Args:
        table: Frozen price table version.
        model: Registry model slug present in ``table``.
        input_tokens: Prompt tokens, excluding cached tokens.
        output_tokens: Completion tokens.
        cached_tokens: Tokens served from the prompt cache.

    Raises:
        KeyError: The model is absent from the table.
        ValueError: A token count is negative.
        TypeError: A token count is not an integer.
    """
    price = table.price_for(model)
    prompt_tokens = _tokens(input_tokens, "input_tokens")
    completion_tokens = _tokens(output_tokens, "output_tokens")
    cache_tokens = _tokens(cached_tokens, "cached_tokens")
    return CostBreakdown(
        model=model,
        price_table_version=table.version,
        basis=price.basis,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cached_tokens=cache_tokens,
        input_cost_usd=prompt_tokens * price.input_per_mtok / _TOKENS_PER_MTOK,
        output_cost_usd=completion_tokens * price.output_per_mtok / _TOKENS_PER_MTOK,
        cached_cost_usd=cache_tokens * price.cached_per_mtok / _TOKENS_PER_MTOK,
    )


def allocate_gpu_cost(
    batch_gpu_cost: float,
    tokens_by_trial: Mapping[str, int],
) -> dict[str, float]:
    """Split a batch's GPU cost across trials by token share (TECH-DESIGN 6.7).

    ``trial.gpu_alloc = batch_gpu_cost x trial_tokens / batch_tokens``; the
    result sums back to ``batch_gpu_cost`` up to floating-point error.

    Args:
        batch_gpu_cost: Total GPU cost of the endpoint for the batch, USD.
        tokens_by_trial: Token count per trial id. The denominator is their sum.

    Raises:
        ValueError: The batch stays empty, the cost is negative, a token count
            is negative, or the tokens sum to zero (the share is undefined).
    """
    if batch_gpu_cost < 0:
        raise ValueError(f"batch_gpu_cost must be non-negative: {batch_gpu_cost!r}")
    if not tokens_by_trial:
        raise ValueError("tokens_by_trial must contain at least one trial")
    tokens = {
        trial_id: _tokens(count, f"tokens_by_trial[{trial_id!r}]")
        for trial_id, count in sorted(tokens_by_trial.items())
    }
    total = math.fsum(tokens.values())
    if total <= 0:
        raise ValueError("tokens_by_trial must sum to a positive token count")
    return {trial_id: batch_gpu_cost * count / total for trial_id, count in tokens.items()}
