"""Tests for the model conformance case library."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.builtin_doubles import (
    FaultAssistedProvider,
    HFDouble,
    StatusMappingProvider,
)
from evalspleef.conformance.case import Case
from evalspleef.conformance.cases_model import (
    CASES,
    HF_PUBLIC_REPO,
)
from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentSpec,
    EndpointHandle,
    EndpointState,
    HFSource,
    ModelSpec,
    ValidationReport,
    VendorSource,
)
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_endpoint import MockApiProvider, MockAssistedProvider
from evalspleef.testing.secrets import DictSecretResolver

EXPECTED_IDS = {
    "M-API-01",
    "M-API-02",
    "M-API-03",
    "M-ID-01",
    "M-LIFE-01",
    "M-LIFE-02",
    "M-LIFE-03",
    "M-LIFE-04",
    "M-ASSIST-01",
    "M-HF-01",
}
SECRET_REF = "vault://models/mock-model"
REVISION = "0" * 40


def _case(case_id: str) -> Case:
    (case,) = [case for case in CASES if case.id == case_id]
    return case


def _run(case: Case, target: ConformanceTarget) -> str | None:
    """Run one case; return the not-applicable reason, or ``None`` when it passed."""
    reason = case.applies(target)
    if reason is not None:
        assert reason, f"{case.id}: a not-applicable verdict needs a reason"
        return reason
    asyncio.run(case.run(target))
    return None


def _spec(base_url: str, model: str, *, source: HFSource | VendorSource | None = None) -> ModelSpec:
    return ModelSpec(
        slug="mock-model",
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=source or VendorSource(vendor="mock", model_id=model),
        deployment=DeploymentSpec(
            adapter="builtin",
            account="default",
            params={"base_url": base_url, "model": model},
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="mock",
        license="proprietary",
        owner="evalspleef",
    )


def _operator_input() -> Mapping[str, str]:
    return {
        "base_url": "https://operator.example/v1",
        "model": "fake-model",
        "deployment_id": "dep-mock-1",
    }


def _api_target(server: FakeOpenAIServer, *, name: str = "test-api") -> ConformanceTarget:
    provider = MockApiProvider(
        "default", {}, DictSecretResolver({SECRET_REF: server.api_key or ""})
    )
    return ConformanceTarget(
        name=name,
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=lambda: _spec(server.base_url, server.model),
            operator_input_factory=None,
            upstream=server,
        ),
    )


class _LenientAssistedProvider(MockAssistedProvider):
    """A broken assisted double: it stores whatever the operator pasted back."""

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        handle.base_url = operator_input.get("base_url")
        handle.upstream_model = operator_input.get("model")
        handle.state = EndpointState.VERIFYING
        return handle


def _mapping_target(provider: StatusMappingProvider | None = None) -> ConformanceTarget:
    active = provider or StatusMappingProvider(
        "default", {}, DictSecretResolver({SECRET_REF: "sk"})
    )

    async def upstream_status(status: int) -> None:
        active.injected_status = status

    return ConformanceTarget(
        name="test-mapping",
        kind="model",
        provider=active,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=lambda: _spec("https://mock.invalid/v1", "fake-model"),
            operator_input_factory=None,
            upstream=None,
        ),
        injectors={"upstream_status": upstream_status},
    )


def _assisted_target(
    provider_cls: type[MockAssistedProvider] = FaultAssistedProvider,
) -> ConformanceTarget:
    provider = provider_cls(
        "default", {"model": "fake-model"}, DictSecretResolver({SECRET_REF: "sk"})
    )

    async def upstream_status(status: int) -> None:
        assert isinstance(provider, FaultAssistedProvider)
        provider.injected_status = status

    return ConformanceTarget(
        name="test-assisted",
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=lambda: _spec("https://mock.invalid/v1", "fake-model"),
            operator_input_factory=_operator_input,
            upstream=None,
        ),
        injectors={"upstream_status": upstream_status},
    )


class _PermissiveHF(HFAccessChecker):
    """A regression double: it accepts every repository."""

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        del repo, token_ref
        return revision

    async def preflight(self, source: HFSource) -> ValidationReport:
        del source
        return ValidationReport(ok=True, checks=(("readable", True, "ok"),))

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        del source
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return source.repo

    async def verify_mirror(self, source: HFSource) -> bool:
        del source
        return True


def _hf_target(checker: HFAccessChecker) -> ConformanceTarget:
    provider = MockApiProvider("default", {}, DictSecretResolver({SECRET_REF: "sk"}))
    source = HFSource(repo=HF_PUBLIC_REPO, revision=REVISION, access="public")
    return ConformanceTarget(
        name="test-hf",
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=lambda: _spec("https://mock.invalid/v1", "fake-model", source=source),
            operator_input_factory=None,
            upstream=None,
            hf_access=checker,
        ),
    )


# --------------------------------------------------------------------------------------
# Library shape and the builtin targets
# --------------------------------------------------------------------------------------


def test_case_ids_are_complete_and_unique() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_IDS
    assert all(case.kind == "model" for case in CASES)
    assert all(case.title for case in CASES)


def test_builtin_model_targets_run_every_applicable_case() -> None:
    model_targets = [target for target in build_targets() if target.kind == "model"]
    assert len(model_targets) == 3

    outcomes: dict[tuple[str, str], str | None] = {}
    try:
        for target in model_targets:
            for case in CASES:
                outcomes[(target.name, case.id)] = _run(case, target)
    finally:
        for target in model_targets:
            if target.cleanup is not None:
                asyncio.run(target.cleanup())

    for (target_name, case_id), reason in outcomes.items():
        if reason is not None:
            assert "management mode" in reason, f"{target_name}/{case_id}: {reason}"
            assert "supplies no" not in reason, f"{target_name}/{case_id}: {reason}"

    api_passes = {
        "M-API-01",
        "M-API-02",
        "M-API-03",
        "M-ID-01",
        "M-LIFE-01",
        "M-LIFE-02",
        "M-LIFE-03",
        "M-LIFE-04",
        "M-HF-01",
    }
    for case_id in api_passes:
        assert outcomes[("mock-model-api", case_id)] is None, case_id
    assert outcomes[("mock-model-api", "M-ASSIST-01")] is not None
    for target_name in ("mock-model-assisted", "mock-model-external"):
        assert outcomes[(target_name, "M-HF-01")] is None, target_name
    assert outcomes[("mock-model-assisted", "M-ASSIST-01")] is None
    for case_id in ("M-LIFE-01", "M-LIFE-02", "M-LIFE-03", "M-LIFE-04"):
        for target_name in ("mock-model-assisted", "mock-model-external"):
            reason = outcomes[(target_name, case_id)]
            assert reason is not None and "api mode" in reason, (target_name, case_id)


def test_lifecycle_cases_apply_only_to_api_mode() -> None:
    by_name = {target.name: target for target in build_targets()}
    for name in ("mock-model-assisted", "mock-model-external"):
        for case in (case for case in CASES if case.id.startswith("M-LIFE")):
            reason = case.applies(by_name[name])
            assert reason is not None and "api mode" in reason


def test_cases_explain_missing_fixtures_on_a_bare_target() -> None:
    provider = MockApiProvider("default", {}, DictSecretResolver({SECRET_REF: "sk"}))
    bare = ConformanceTarget(
        name="bare",
        kind="model",
        provider=provider,
        fake=True,
        model_fixture=ModelFixture(
            spec_factory=lambda: _spec("https://mock.invalid/v1", "fake-model"),
            operator_input_factory=None,
            upstream=None,
        ),
    )
    api_reason = _case("M-API-01").applies(bare)
    assert api_reason is not None and "upstream fixture" in api_reason
    mapping_reason = _case("M-API-03").applies(bare)
    assert mapping_reason is not None and "upstream_status" in mapping_reason
    hf_reason = _case("M-HF-01").applies(bare)
    assert hf_reason is not None and "HF access fixture" in hf_reason
    assist_reason = _case("M-ASSIST-01").applies(bare)
    assert assist_reason is not None and "management mode" in assist_reason


# --------------------------------------------------------------------------------------
# M-API / M-ID against a scripted upstream
# --------------------------------------------------------------------------------------


def test_api_and_identity_cases_pass_against_scripted_upstream() -> None:
    with FakeOpenAIServer(api_key="sk-conformance") as server:
        target = _api_target(server)
        for case_id in ("M-API-01", "M-API-02", "M-ID-01"):
            assert _run(_case(case_id), target) is None, case_id


def test_error_mapping_case_needs_the_fault_injector() -> None:
    with FakeOpenAIServer(api_key="sk-conformance") as server:
        reason = _case("M-API-03").applies(_api_target(server))
    assert reason is not None and "upstream_status" in reason


def test_error_mapping_case_passes_with_a_mapping_provider() -> None:
    assert _run(_case("M-API-03"), _mapping_target()) is None


def test_error_mapping_case_detects_swallowed_upstream_errors() -> None:
    provider = StatusMappingProvider("default", {}, DictSecretResolver({SECRET_REF: "sk"}))
    provider.swallow = True
    with pytest.raises(AssertionError, match="not surfaced"):
        _run(_case("M-API-03"), _mapping_target(provider))


def test_identity_case_detects_reported_model_mismatch() -> None:
    server = FakeOpenAIServer(api_key="sk-conformance", reported_model="impostor")
    with server, pytest.raises(AssertionError, match="reports model"):
        _run(_case("M-ID-01"), _api_target(server))


# --------------------------------------------------------------------------------------
# M-ASSIST
# --------------------------------------------------------------------------------------


def test_assisted_case_passes_with_full_fixture() -> None:
    assert _run(_case("M-ASSIST-01"), _assisted_target()) is None


def test_assisted_case_detects_an_accepted_wrong_base_url() -> None:
    target = _assisted_target(_LenientAssistedProvider)
    with pytest.raises(AssertionError, match="plain-http base_url"):
        _run(_case("M-ASSIST-01"), target)


# --------------------------------------------------------------------------------------
# M-HF
# --------------------------------------------------------------------------------------


def test_hf_case_passes_with_supplied_fixtures() -> None:
    assert _run(_case("M-HF-01"), _hf_target(HFDouble())) is None


def test_hf_case_detects_a_double_that_accepts_gated_repos() -> None:
    with pytest.raises(AssertionError, match="accepted the gated scenario"):
        _run(_case("M-HF-01"), _hf_target(_PermissiveHF()))


# --------------------------------------------------------------------------------------
# Sandbox targets never run model cases
# --------------------------------------------------------------------------------------


def test_model_cases_do_not_apply_to_sandbox_targets() -> None:
    (sandbox,) = [target for target in build_targets() if target.kind == "sandbox"]
    for case in CASES:
        reason = case.applies(sandbox)
        assert reason is not None and "model" in reason
