"""evalspleef.core.agreement — judge agreement metrics (PRD appendix C.5 11.3, AC-09, NFR-01).

Pure functions, standard library only: no clock, no randomness, no global state
(AGENTS.md rule 7), so the same input always yields the same output. Threshold
sources:

- ``PERCENT_GATE`` / ``ADJACENT_GATE``: C.5 11.3 "calibration gate ≥0.70; adjacent-tier
  agreement ≥0.90";
- ``KAPPA_GATE``: AC-09 "judge calibration κ≥0.70" (Landis & Koch 1977 bands:
  ≥0.61 substantial, ≥0.81 almost perfect, PRD C.5 11.3);
- ``ALPHA_USABLE`` / ``ALPHA_EXPLORATORY``: C.5 11.3 "α≥0.800 usable;
  0.667≤α<0.800 exploratory conclusions only; <0.667 unacceptable"
  (Krippendorff's empirical thresholds);
- ``REGRADE_GATE``: AC-09 "tier regrade agreement ≥95%";
- ``STABILITY_GATE``: NFR-01 "share of reruns on the same config whose tier
  differs by ≤ 1 tier ≥ 90%".

Degenerate inputs: empty input raises ``ValueError``; a metric that is
mathematically undefined (κ with pe=1, α with De=0, i.e. both raters used a
single category and agreed completely) raises ``AgreementUndefinedError`` - the
caller (CLI) counts it as "not enough data" rather than a zero score.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ADJACENT_GATE",
    "ALPHA_EXPLORATORY",
    "ALPHA_USABLE",
    "KAPPA_GATE",
    "PERCENT_GATE",
    "REGRADE_GATE",
    "STABILITY_GATE",
    "AgreementResult",
    "AgreementUndefinedError",
    "adjacent_agreement",
    "alpha_verdict",
    "cohen_kappa",
    "krippendorff_alpha",
    "percent_agreement",
]

#: Exact-agreement calibration gate (C.5 11.3).
PERCENT_GATE = 0.70
#: Adjacent-tier agreement gate (C.5 11.3).
ADJACENT_GATE = 0.90
#: Judge-calibration κ gate (AC-09, inherited from v2).
KAPPA_GATE = 0.70
#: α ≥ 0.800: conclusions usable (C.5 11.3).
ALPHA_USABLE = 0.800
#: α ≥ 0.667: exploratory conclusions only (C.5 11.3).
ALPHA_EXPLORATORY = 0.667
#: Tier-regrade agreement gate (AC-09, inherited from v2).
REGRADE_GATE = 0.95
#: Share of reruns on the same config within ≤1 tier (NFR-01).
STABILITY_GATE = 0.90

#: Frozen spellings of the weighting modes.
Weighting = Literal["unweighted", "linear", "quadratic"]
#: Frozen spellings of the α difference metric.
AlphaMetric = Literal["nominal", "ordinal", "interval"]


class AgreementUndefinedError(ValueError):
    """The metric is undefined for this degenerate input (a single category gives κ pe=1 or α De=0)."""


@dataclass(frozen=True)
class AgreementResult:
    """One agreement computation: metric value, sample count and gate verdict."""

    metric: str
    value: float
    n: int
    threshold: float | None = None
    note: str | None = None

    @property
    def passed(self) -> bool | None:
        """Gate verdict; None when there is no threshold."""
        if self.threshold is None:
            return None
        return self.value >= self.threshold

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready dict."""
        payload: dict[str, object] = {
            "metric": self.metric,
            "value": self.value,
            "n": self.n,
            "threshold": self.threshold,
            "passed": self.passed,
        }
        if self.note:
            payload["note"] = self.note
        return payload


def _materialize(pairs: Iterable[tuple[Hashable, Hashable]]) -> list[tuple[Hashable, Hashable]]:
    materialized = list(pairs)
    if not materialized:
        raise ValueError("agreement needs at least one pair of observations")
    return materialized


def percent_agreement(
    pairs: Iterable[tuple[Hashable, Hashable]],
    *,
    threshold: float | None = PERCENT_GATE,
) -> AgreementResult:
    """Exact agreement: pairs whose verdicts match / all pairs (C.5 11.3)."""
    materialized = _materialize(pairs)
    agreeing = sum(1 for left, right in materialized if left == right)
    return AgreementResult(
        metric="percent_agreement",
        value=agreeing / len(materialized),
        n=len(materialized),
        threshold=threshold,
    )


def adjacent_agreement(
    pairs: Iterable[tuple[Hashable, Hashable]],
    values: Mapping[Hashable, float],
    *,
    threshold: float | None = ADJACENT_GATE,
) -> AgreementResult:
    """Adjacent-tier agreement: share of pairs whose ordered categories (tiers) differ by ≤1 tier
    (C.5 11.3).

    ``values`` maps categories to numbers (tiers use ``TIER_VALUE``: P=7 … F=0).
    """
    materialized = _materialize(pairs)
    within = 0
    for left, right in materialized:
        if left not in values or right not in values:
            raise ValueError(f"category has no numeric mapping: {left!r} / {right!r}")
        if abs(values[left] - values[right]) <= 1:
            within += 1
    return AgreementResult(
        metric="adjacent_agreement",
        value=within / len(materialized),
        n=len(materialized),
        threshold=threshold,
    )


def _ordinal_positions(
    categories: Iterable[Hashable],
    values: Mapping[Hashable, float] | None,
) -> dict[Hashable, int]:
    """Map categories to ordinal positions 0..C-1; weighted-kappa weights use the position gap."""
    distinct = set(categories)
    if values is not None:
        missing = [category for category in distinct if category not in values]
        if missing:
            raise ValueError(f"category has no numeric mapping: {sorted(map(str, missing))}")
        ordered = sorted(distinct, key=lambda category: values[category])
    else:
        ordered = sorted(distinct, key=str)
    return {category: position for position, category in enumerate(ordered)}


def cohen_kappa(
    pairs: Iterable[tuple[Hashable, Hashable]],
    *,
    weighting: Weighting = "unweighted",
    values: Mapping[Hashable, float] | None = None,
    threshold: float | None = KAPPA_GATE,
) -> AgreementResult:
    """Cohen κ (Cohen 1960); weighted kappa (Cohen 1968) with ``linear``/``quadratic``.

    - unweighted: κ=(po−pe)/(1−pe), with pe the product of the two marginals;
    - linear/quadratic: κ=1−Σw·Õ/Σw·Ẽ, w from the ordinal position gap (|i−j| or (i−j)²),
      ordered by ``values`` (tiers use ``TIER_VALUE``; C.5 11.3: tiers are ordered
      categories, so ordinal weights are more sensitive).

    κ is undefined when pe=1 (both raters used a single category); raises
    ``AgreementUndefinedError``.
    """
    materialized = _materialize(pairs)
    if weighting not in ("unweighted", "linear", "quadratic"):
        raise ValueError(f"weighting must be unweighted/linear/quadratic, got {weighting!r}")
    if weighting != "unweighted" and values is None:
        raise ValueError("weighted kappa needs a values mapping to order the categories")

    categories = _ordinal_positions(
        (category for pair in materialized for category in pair), values
    )
    size = len(categories)
    observed = [[0.0] * size for _ in range(size)]
    for left, right in materialized:
        observed[categories[left]][categories[right]] += 1
    total = float(len(materialized))
    row_sums = [sum(row) for row in observed]
    col_sums = [sum(observed[row][col] for row in range(size)) for col in range(size)]

    if weighting == "unweighted":
        po = sum(observed[index][index] for index in range(size)) / total
        pe = sum(row_sums[index] * col_sums[index] for index in range(size)) / (total * total)
        if pe >= 1.0:
            raise AgreementUndefinedError(
                "pe=1 (both raters used a single category), κ is undefined"
            )
        return AgreementResult(
            metric="cohen_kappa",
            value=(po - pe) / (1.0 - pe),
            n=len(materialized),
            threshold=threshold,
        )

    def weight(i: int, j: int) -> float:
        distance = abs(i - j)
        return float(distance if weighting == "linear" else distance * distance)

    observed_weighted = 0.0
    expected_weighted = 0.0
    for i in range(size):
        for j in range(size):
            w = weight(i, j)
            observed_weighted += w * observed[i][j] / total
            expected_weighted += w * (row_sums[i] / total) * (col_sums[j] / total)
    if expected_weighted <= 0.0:
        raise AgreementUndefinedError(
            "expected disagreement is 0 (both raters used a single category), weighted κ is undefined"
        )
    return AgreementResult(
        metric=f"cohen_kappa_{weighting}",
        value=1.0 - observed_weighted / expected_weighted,
        n=len(materialized),
        threshold=threshold,
    )


def alpha_verdict(value: float) -> str:
    """Empirical α verdict (C.5 11.3): usable / exploratory / unacceptable."""
    if value >= ALPHA_USABLE:
        return "usable"
    if value >= ALPHA_EXPLORATORY:
        return "exploratory"
    return "unacceptable"


def krippendorff_alpha(
    units: Iterable[Iterable[Hashable | None]],
    *,
    metric: AlphaMetric = "nominal",
    threshold: float | None = ALPHA_EXPLORATORY,
) -> AgreementResult:
    """Krippendorff α = 1 − Do/De (Hayes & Krippendorff 2007; Krippendorff 2011).

    ``units`` holds, per judgement unit (one rubric item), each rater's verdict
    sequence, with ``None`` for missing values. Any number of raters, missing data
    supported. ``metric`` selects the difference function δ²:

    - nominal: 1 for different categories, 0 for the same;
    - ordinal: squared midrank difference, δ²_ck = ((n_c+n_k)/2 + Σ_{c<g<k} n_g)²;
    - interval: δ²_ck = (c−k)², categories must be numeric (tiers go through
      ``TIER_VALUE`` first).

    α is undefined when De=0 (every pairable value is identical); raises
    ``AgreementUndefinedError``.
    """
    if metric not in ("nominal", "ordinal", "interval"):
        raise ValueError(f"metric must be nominal/ordinal/interval, got {metric!r}")
    materialized = [tuple(unit) for unit in units]
    if not materialized:
        raise ValueError("α needs at least one judgement unit")

    # Coincidence matrix: a unit holding m_u values contributes m_u(m_u−1) ordered
    # pairs (distinct positions), each pair counted 1/(m_u−1); every pairable value
    # enters the marginal n_c exactly once.
    coincidences: dict[tuple[Hashable, Hashable], float] = {}
    value_count: dict[Hashable, float] = {}
    pairable = 0
    for unit in materialized:
        present = [value for value in unit if value is not None]
        if len(present) < 2:
            continue
        pairable += len(present)
        share = 1.0 / (len(present) - 1)
        for value in present:
            value_count[value] = value_count.get(value, 0.0) + 1.0
        for i, left in enumerate(present):
            for j, right in enumerate(present):
                if i == j:
                    continue
                key = (left, right)
                coincidences[key] = coincidences.get(key, 0.0) + share
    if pairable < 2:
        raise ValueError("α needs at least two pairable values")

    if metric == "interval":
        numeric: dict[Hashable, float] = {}
        for value in value_count:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"interval metric needs numeric categories, got {value!r}")
            numeric[value] = float(value)
        ordered_values: list[Hashable] = sorted(value_count, key=lambda v: numeric[v])
    else:
        ordered_values = sorted(value_count, key=str)

    def delta_squared(left: Hashable, right: Hashable) -> float:
        if left == right:
            return 0.0
        if metric == "nominal":
            return 1.0
        if metric == "interval":
            diff = numeric[left] - numeric[right]
            return diff * diff
        # ordinal: squared midrank difference (Krippendorff 2011 §D).
        low, high = (
            (left, right)
            if ordered_values.index(left) <= ordered_values.index(right)
            else (right, left)
        )
        low_rank = ordered_values.index(low)
        high_rank = ordered_values.index(high)
        between = sum(
            value_count[ordered_values[index]] for index in range(low_rank + 1, high_rank)
        )
        rank_diff = (value_count[low] + value_count[high]) / 2.0 + between
        return rank_diff * rank_diff

    observed = 0.0
    expected = 0.0
    distinct = list(value_count)
    for left in distinct:
        for right in distinct:
            weight = delta_squared(left, right)
            if weight == 0.0:
                continue
            observed += coincidences.get((left, right), 0.0) * weight
            expected += value_count[left] * value_count[right] * weight
    observed /= pairable
    expected /= pairable * (pairable - 1)
    if expected <= 0.0:
        raise AgreementUndefinedError("De=0 (every pairable value is identical), α is undefined")
    return AgreementResult(
        metric=f"krippendorff_alpha_{metric}",
        value=1.0 - observed / expected,
        n=pairable,
        threshold=threshold,
    )
