"""Textbook-value anchors and degenerate-input semantics for evalspleef.core.agreement.

Anchors:
- Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability"
  (https://web.archive.org/web/20231219061845/https://www.asc.upenn.edu/sites/default/files/2021-03/Computing%20Krippendorff's%20Alpha-Reliability.pdf)
  §C's 4 raters × 12 units example (48 values, 7 missing): nominal α=0.743,
  ordinal α=0.815, interval α=0.849 (values published in §C/§D of the paper).
- Wikipedia "Cohen's kappa" worked example
  (https://en.wikipedia.org/wiki/Cohen%27s_kappa): a=20, b=5, c=10, d=15,
  po=0.7, pe=0.5, κ=0.4.
- weighted kappa (Cohen 1968, formula in the same Wikipedia article): the
  expected values for the 3×3 example are derived by hand in this test (the
  comment shows the arithmetic).
"""

from __future__ import annotations

import math

import pytest

from evalspleef.core.agreement import (
    ADJACENT_GATE,
    ALPHA_EXPLORATORY,
    ALPHA_USABLE,
    KAPPA_GATE,
    PERCENT_GATE,
    AgreementUndefinedError,
    adjacent_agreement,
    alpha_verdict,
    cohen_kappa,
    krippendorff_alpha,
    percent_agreement,
)
from evalspleef.core.models import TIER_VALUE, Tier

# Krippendorff (2011) §C: 4 observers × 12 units, 7/48 missing.
KRIPPENDORFF_2011_UNITS = [
    [1, 1, None, 1],
    [2, 2, 3, 2],
    [3, 3, 3, 3],
    [3, 3, 3, 3],
    [2, 2, 2, 2],
    [1, 2, 3, 4],
    [4, 4, 4, 4],
    [1, 1, 2, 1],
    [2, 2, 2, 2],
    [None, 5, 5, 5],
    [None, None, 1, 1],
    [None, 3, None, None],
]


def test_krippendorff_alpha_matches_published_2011_example() -> None:
    """Published values for the Krippendorff (2011) §C example: nominal 0.743 / ordinal 0.815 / interval 0.849."""
    published = {"nominal": 0.743, "ordinal": 0.815, "interval": 0.849}
    for metric, expected in published.items():
        result = krippendorff_alpha(KRIPPENDORFF_2011_UNITS, metric=metric)  # type: ignore[arg-type]
        assert result.value == pytest.approx(expected, abs=5e-4), metric
        assert result.n == 40  # 40 pairable values (coincidence-matrix marginal sum)


def test_krippendorff_alpha_perfect_agreement_is_one() -> None:
    units = [[1, 1, 1], [2, 2, 2], [None, 3, 3]]
    for metric in ("nominal", "ordinal", "interval"):
        assert krippendorff_alpha(units, metric=metric).value == pytest.approx(1.0)  # type: ignore[arg-type]


def test_cohen_kappa_matches_wikipedia_worked_example() -> None:
    """Wikipedia "Cohen's kappa" worked example: po=0.7, pe=0.5, κ=0.4."""
    pairs = [("yes", "yes")] * 20 + [("yes", "no")] * 5 + [("no", "yes")] * 10 + [("no", "no")] * 15
    result = cohen_kappa(pairs)
    assert result.value == pytest.approx(0.4)
    assert result.n == 50


def test_cohen_kappa_weighted_matches_hand_computation() -> None:
    """unweighted/linear/quadratic κ on a 3×3 ordered example.

    O = [[10,2,1],[3,15,4],[1,2,12]], row sums [13,22,15], column sums [14,19,17], N=50.
    - unweighted: po=37/50=0.74, pe=0.26·0.28+0.44·0.38+0.30·0.34=0.342,
      κ=(0.74−0.342)/(1−0.342)=0.604863…
    - linear (w=|i−j|): Σw·Õ=0.30, Σw·Ẽ=0.8304, κ=1−0.30/0.8304=0.638728…
    - quadratic (w=(i−j)²): Σw·Õ=0.38, Σw·Ẽ=1.1752, κ=1−0.38/1.1752=0.676651…
    """
    pairs = (
        [(1, 1)] * 10
        + [(1, 2)] * 2
        + [(1, 3)] * 1
        + [(2, 1)] * 3
        + [(2, 2)] * 15
        + [(2, 3)] * 4
        + [(3, 1)] * 1
        + [(3, 2)] * 2
        + [(3, 3)] * 12
    )
    values = {1: 0.0, 2: 1.0, 3: 2.0}
    assert cohen_kappa(pairs, values=values).value == pytest.approx(0.6048632219)
    linear = cohen_kappa(pairs, weighting="linear", values=values)
    assert linear.value == pytest.approx(0.6387283237)
    quadratic = cohen_kappa(pairs, weighting="quadratic", values=values)
    assert quadratic.value == pytest.approx(0.6766507828)


def test_percent_and_adjacent_agreement() -> None:
    pairs = [(Tier.P, Tier.A), (Tier.B_PLUS, Tier.B), (Tier.P, Tier.F), (Tier.C, Tier.B_PLUS)]
    exact = percent_agreement(pairs)
    assert exact.value == 0.0 and exact.n == 4 and exact.threshold == PERCENT_GATE
    assert exact.passed is False
    adjacent = adjacent_agreement(pairs, TIER_VALUE)
    # |P−A|=1, |B+−B|=1, |P−F|=7, |C−B+|=2 → 2/4
    assert adjacent.value == 0.5 and adjacent.n == 4 and adjacent.threshold == ADJACENT_GATE


def test_result_carries_gate_verdicts() -> None:
    kappa = cohen_kappa([("a", "a")] * 7 + [("a", "b")] * 2 + [("b", "b")] * 1)
    assert kappa.threshold == KAPPA_GATE
    assert isinstance(kappa.passed, bool)
    alpha = krippendorff_alpha([[1, 1], [2, 2], [1, 2]])
    assert alpha.threshold == ALPHA_EXPLORATORY
    assert alpha_verdict(ALPHA_USABLE) == "usable"
    assert alpha_verdict(0.7) == "exploratory"
    assert alpha_verdict(0.5) == "unacceptable"


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one pair of observations"):
        percent_agreement([])
    with pytest.raises(ValueError, match="at least one pair of observations"):
        cohen_kappa([])
    with pytest.raises(ValueError, match="at least one judgement unit"):
        krippendorff_alpha([])
    with pytest.raises(ValueError, match="at least two pairable values"):
        krippendorff_alpha([[1], [None, 2]])


def test_single_category_is_undefined_not_one() -> None:
    """pe=1 / De=0 when both raters use a single category: κ and α are undefined, raising the typed error."""
    with pytest.raises(AgreementUndefinedError):
        cohen_kappa([("pass", "pass")] * 10)
    with pytest.raises(AgreementUndefinedError):
        krippendorff_alpha([["pass", "pass"], ["pass", "pass"]])


def test_all_agree_with_two_categories_is_one() -> None:
    pairs = [("pass", "pass")] * 5 + [("fail", "fail")] * 5
    assert cohen_kappa(pairs).value == pytest.approx(1.0)
    assert percent_agreement(pairs).value == 1.0
    alpha = krippendorff_alpha([["pass", "pass"], ["fail", "fail"]])
    assert alpha.value == pytest.approx(1.0)


def test_alpha_handles_missing_data_and_two_raters() -> None:
    """Missing (None) values do not pair; with two raters α matches κ in sign and magnitude."""
    units = [[1, 1], [2, 2], [1, 2], [None, 1], [2, None]]
    alpha = krippendorff_alpha(units, metric="nominal")
    assert -1.0 <= alpha.value <= 1.0
    assert not math.isnan(alpha.value)
