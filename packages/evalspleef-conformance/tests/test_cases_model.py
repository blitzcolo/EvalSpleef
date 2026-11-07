"""Tests for the model conformance case library."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.case import Case
from evalspleef.conformance.cases_model import (
    CASES,
    HF_BIN_ONLY_REPO,
    HF_GATED_REPO,
    HF_PRIVATE_REPO,
    HF_PUBLIC_REPO,
)
from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
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


class _StatusMappingProvider(MockApiProvider):
    """Api-mode double that maps an injected upstream status onto the SPI error taxonomy."""

    injected_status: int | None = None
    swallow: bool = False

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        del timeout_s
        status = self.injected_status
        self.injected_status = None
        if self.swallow or status is None:
            return handle.state
        if status in (429, 500, 503):
            raise EndpointUnavailable(f"upstream {status}", detail={"status": status})
        raise EndpointMismatch(f"upstream {status}", detail={"status": status})


def _mapping_target(provider: _StatusMappingProvider | None = None) -> ConformanceTarget:
    active = provider or _StatusMappingProvider(
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


class _FaultAssistedProvider(MockAssistedProvider):
    """Assisted double that surfaces a scripted vendor 403 from ``attach``."""

    injected_status: int | None = None

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        status = self.injected_status
        self.injected_status = None
        if status in (401, 403):
            raise EndpointMismatch(
                "the vendor rejected the attached deployment", detail={"status": status}
            )
        return await super().attach(handle, operator_input)


class _LenientAssistedProvider(MockAssistedProvider):
    """A broken assisted double: it stores whatever the operator pasted back."""

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        handle.base_url = operator_input.get("base_url")
        handle.upstream_model = operator_input.get("model")
        handle.state = EndpointState.VERIFYING
        return handle


def _assisted_target(
    provider_cls: type[MockAssistedProvider] = _FaultAssistedProvider,
) -> ConformanceTarget:
    provider = provider_cls(
        "default", {"model": "fake-model"}, DictSecretResolver({SECRET_REF: "sk"})
    )

    async def upstream_status(status: int) -> None:
        assert isinstance(provider, _FaultAssistedProvider)
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


class _HFDouble(HFAccessChecker):
    """HF access double for the four fixture repos M-HF-01 probes."""

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        del repo, token_ref
        return revision

    async def preflight(self, source: HFSource) -> ValidationReport:
        if source.repo == HF_PUBLIC_REPO:
            return ValidationReport(ok=True, checks=(("readable", True, "public repo readable"),))
        if source.repo == HF_GATED_REPO:
            return ValidationReport(
                ok=False,
                checks=(("licence", False, "gated repository licence has not been accepted"),),
            )
        if source.repo == HF_PRIVATE_REPO:
            return ValidationReport(
                ok=False,
                checks=(
                    (
                        "access",
                        False,
                        "private repository is not readable by the deployment identity",
                    ),
                ),
            )
        if source.repo == HF_BIN_ONLY_REPO:
            return ValidationReport(
                ok=False,
                checks=(
                    (
                        "weights_format",
                        False,
                        "repository ships .bin weights only; safetensors are required",
                    ),
                ),
            )
        raise AssertionError(f"unexpected HF repo {source.repo!r}")

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        del source
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return source.mirror_repo or source.repo

    async def verify_mirror(self, source: HFSource) -> bool:
        del source
        return True


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


def test_builtin_model_targets_pass_or_are_not_applicable() -> None:
    model_targets = [target for target in build_targets() if target.kind == "model"]
    assert len(model_targets) == 3

    outcomes: dict[tuple[str, str], str | None] = {}
    for target in model_targets:
        for case in CASES:
            outcomes[(target.name, case.id)] = _run(case, target)

    for (target_name, case_id), reason in outcomes.items():
        if reason is not None:
            assert reason, f"{target_name}/{case_id}: empty not-applicable reason"

    lifecycle_ids = {"M-LIFE-01", "M-LIFE-02", "M-LIFE-03", "M-LIFE-04"}
    for case_id in lifecycle_ids:
        assert outcomes[("mock-model-api", case_id)] is None, case_id
    for target_name in ("mock-model-assisted", "mock-model-external"):
        for case in CASES:
            assert outcomes[(target_name, case.id)] is not None, (target_name, case.id)
    for case_id in EXPECTED_IDS - lifecycle_ids:
        assert outcomes[("mock-model-api", case_id)] is not None, case_id


def test_lifecycle_cases_apply_only_to_api_mode() -> None:
    by_name = {target.name: target for target in build_targets()}
    for name in ("mock-model-assisted", "mock-model-external"):
        for case in (case for case in CASES if case.id.startswith("M-LIFE")):
            reason = case.applies(by_name[name])
            assert reason is not None and "api mode" in reason


def test_hf_and_assisted_cases_explain_missing_fixtures() -> None:
    by_name = {target.name: target for target in build_targets()}
    hf_reason = _case("M-HF-01").applies(by_name["mock-model-api"])
    assert hf_reason is not None and "HF access fixture" in hf_reason
    assist_reason = _case("M-ASSIST-01").applies(by_name["mock-model-assisted"])
    assert assist_reason is not None and "injector" in assist_reason


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
    provider = _StatusMappingProvider("default", {}, DictSecretResolver({SECRET_REF: "sk"}))
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
    assert _run(_case("M-HF-01"), _hf_target(_HFDouble())) is None


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
