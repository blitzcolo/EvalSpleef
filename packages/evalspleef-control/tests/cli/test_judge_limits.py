from __future__ import annotations

from typing import Any

from evalspleef.control.cli.context import judge_limits
from evalspleef.control.observe.judging import JUDGE_MAX_TOKENS, JUDGE_TIMEOUT_S
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec


def _spec(params: dict[str, Any]) -> ModelSpec:
    return ModelSpec(
        slug="j",
        spec_version=1,
        family="f",
        roles=frozenset({"judge"}),
        source=HFSource(repo="o/r", revision="a" * 40, access="public", files_sha256={}),
        deployment=DeploymentSpec(adapter="a", account="a", params=params),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="api:claude@2026-11",
        license="mit",
        owner="o",
    )


def test_judge_params_raise_the_client_limits() -> None:
    assert judge_limits(_spec({"judge_max_tokens": 8192, "judge_timeout_s": 180})) == (8192, 180.0)


def test_without_judge_params_the_client_defaults_stand() -> None:
    assert judge_limits(_spec({})) == (JUDGE_MAX_TOKENS, JUDGE_TIMEOUT_S)
