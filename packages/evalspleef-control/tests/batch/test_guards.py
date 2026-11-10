"""FR-FLOW-01 freeze guards and FR-FLOW-02 / FR-MOD-06 run guards through the batch machine."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from evalspleef.control.batch import JudgeGuardFacts, ProviderEndpointStates
from evalspleef.control.scheduling import CapacitySpec
from evalspleef.core.batch import BatchGuardFailed, BatchState
from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry
from evalspleef.spi.model import DeploymentSpec, EndpointHandle, EndpointState, HFSource, ModelSpec
from evalspleef.spi.sandbox import TemplateRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_endpoint import MockApiProvider
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver

SECRET = {"vault://models/mock-32b": "secret-value"}


def freeze_scenario(name: str, task_factory: Any, model_factory: Any) -> dict[str, Any]:
    """Build arguments that break exactly the freeze guard called ``name``."""
    match name:
        case "lint_passed" | "budget_approved":
            return {}
        case "budget_over_cap":
            return {"budget_cap": 10.0}
        case "image_digests_locked":
            task = task_factory("web-001")
            unpinned = replace(task, template=TemplateRef("oci", task.template.ref, "latest"))
            return {"tasks": [unpinned]}
        case "endpoint_probe_reports_frozen":
            return {"models": [model_factory("alpha", probe_report_id=None)]}
        case "model_revisions_frozen":
            return {"models": [model_factory("alpha", self_deployed=True)]}
        case "gateway_version_frozen":
            return {"gateway_version": ""}
        case "template_digest_map_frozen":
            return {"template_digests": {}}
    raise AssertionError(name)


@pytest.mark.parametrize(
    ("name", "lint", "approved", "condition"),
    [
        ("lint_passed", False, True, "lint_passed"),
        ("budget_approved", True, False, "budget_approved"),
        ("budget_over_cap", True, True, "budget_approved"),
        ("image_digests_locked", True, True, "image_digests_locked"),
        ("endpoint_probe_reports_frozen", True, True, "endpoint_probe_reports_frozen"),
        ("model_revisions_frozen", True, True, "model_revisions_frozen"),
        ("gateway_version_frozen", True, True, "gateway_version_frozen"),
        ("template_digest_map_frozen", True, True, "template_digest_map_frozen"),
    ],
)
async def test_freeze_is_refused_when_a_fr_flow_01_guard_fails(
    build, task_factory, model_factory, name: str, lint: bool, approved: bool, condition: str
) -> None:
    h = await build(**freeze_scenario(name, task_factory, model_factory))

    with pytest.raises(BatchGuardFailed) as refused:
        h.driver.freeze(lint_passed=lint, budget_approved=approved)

    assert refused.value.failed_conditions == (condition,)
    assert h.driver.state is BatchState.DRAFT


async def test_every_failed_freeze_guard_is_reported_together(build) -> None:
    h = await build(gateway_version="")

    with pytest.raises(BatchGuardFailed) as refused:
        h.driver.freeze(lint_passed=False, budget_approved=True)

    assert refused.value.failed_conditions == ("lint_passed", "gateway_version_frozen")


async def test_freeze_succeeds_when_everything_is_pinned(build) -> None:
    h = await build(n_models=2, n_tasks=2)

    payload = h.driver.freeze(lint_passed=True, budget_approved=True)

    assert payload["transition"] == "draft->frozen"
    assert h.driver.state is BatchState.FROZEN


def judge_fact(slug: str = "judge-a", **changes: Any) -> JudgeGuardFacts:
    base = JudgeGuardFacts(
        slug=slug,
        endpoint_id=f"ep_{slug}_00000000",
        state=EndpointState.READY,
        report_id=f"pr_{slug}",
        failed_probes=(),
    )
    return replace(base, **changes)


async def test_freeze_is_refused_when_a_judge_required_probe_failed(build) -> None:
    h = await build()
    facts = {
        "gemini-judge": judge_fact(
            "gemini-judge", state=EndpointState.PROBE_FAILED, failed_probes=("P-14",)
        )
    }

    with pytest.raises(BatchGuardFailed) as refused:
        h.driver.freeze(lint_passed=True, budget_approved=True, judge_facts=facts)

    assert refused.value.failed_conditions == ("judges_admitted",)
    reason = refused.value.failures[0].reason
    assert "gemini-judge" in reason
    assert "probe_failed" in reason
    assert "P-14" in reason
    assert h.driver.state is BatchState.DRAFT
    assert h.executor.calls == []


async def test_freeze_is_refused_when_a_judge_is_not_registered(build) -> None:
    h = await build()
    facts = {"judge-a": judge_fact("judge-a", endpoint_id=None, state=None, report_id=None)}

    with pytest.raises(BatchGuardFailed) as refused:
        h.driver.freeze(lint_passed=True, budget_approved=True, judge_facts=facts)

    assert refused.value.failed_conditions == ("judges_admitted",)
    assert "judge judge-a is not registered" in refused.value.failures[0].reason


async def test_freeze_is_refused_when_a_judge_has_no_probe_report(build) -> None:
    h = await build()
    facts = {"judge-a": judge_fact("judge-a", report_id=None)}

    with pytest.raises(BatchGuardFailed) as refused:
        h.driver.freeze(lint_passed=True, budget_approved=True, judge_facts=facts)

    assert refused.value.failed_conditions == ("judges_admitted",)
    assert "has no probe report" in refused.value.failures[0].reason


async def test_freeze_succeeds_when_the_judge_panel_is_admitted(build) -> None:
    h = await build()
    facts = {"judge-a": judge_fact(), "judge-b": judge_fact("judge-b", state=EndpointState.WARM)}

    payload = h.driver.freeze(lint_passed=True, budget_approved=True, judge_facts=facts)

    assert payload["transition"] == "draft->frozen"
    assert h.driver.state is BatchState.FROZEN


def mock_model_spec() -> ModelSpec:
    return ModelSpec(
        slug="mock-32b",
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=HFSource(repo="org/mock-32b", revision="0" * 40, access="public"),
        deployment=DeploymentSpec(adapter="mock-api", account="acct", params={}),
        declared_context_tokens=32768,
        declared_max_output_tokens=4096,
        pricing_ref="ref:mock/32b",
        license="apache-2.0",
        owner="qa",
    )


async def deploy_endpoint() -> tuple[MockApiProvider, EndpointHandle]:
    provider = MockApiProvider(
        "acct",
        {
            "base_url": "https://api.example.test/v1",
            "model": "mock-32b-upstream",
            "api_key_ref": "vault://models/mock-32b",
            "cold_start_s": 0,
        },
        DictSecretResolver(SECRET),
        clock=FakeClock(),
    )
    spec = mock_model_spec()
    return provider, await provider.deploy(spec, await provider.plan(spec))


@pytest.mark.parametrize("cooled", [False, True])
async def test_running_is_refused_until_the_self_deployed_endpoint_is_warm(
    build, model_factory, cooled: bool
) -> None:
    provider, handle = await deploy_endpoint()  # deployed and ready, but not warmed up
    if cooled:
        await provider.wake(handle, 60)
        await provider.teardown(handle, "sleep")
    model = model_factory(
        "mock-32b",
        endpoint_id=handle.endpoint_id,
        self_deployed=True,
        revision="0" * 40,
        probe_report_id="pr_1",
    )
    h = await build(
        models=[model],
        endpoint_states=ProviderEndpointStates({handle.endpoint_id: (provider, handle)}),
    )
    h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await h.driver.run()

    assert refused.value.failed_conditions == ("endpoints_ready_warm",)
    assert handle.endpoint_id in refused.value.failures[0].reason
    assert str(handle.state) in refused.value.failures[0].reason
    assert h.driver.state is BatchState.FROZEN
    assert h.executor.calls == []
    assert h.providers["docker-a"].created_sandboxes() == []

    await provider.wake(handle, 60)
    await h.driver.start()
    assert h.driver.state is BatchState.RUNNING


async def test_hosted_endpoints_are_not_checked_for_warmth(build) -> None:
    h = await build()  # the stub states object fails the test if it is asked about an endpoint
    h.driver.freeze(lint_passed=True, budget_approved=True)

    await h.driver.start()

    assert h.driver.state is BatchState.RUNNING


async def test_capacity_that_can_never_host_the_task_blocks_running(build, task_factory) -> None:
    h = await build(
        tasks=[task_factory("web-001", cpus=64)],
        capacity=[CapacitySpec("docker-a", cpus=8.0, memory_mb=65536)],
    )
    h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await h.driver.start()

    assert refused.value.failed_conditions == ("carrier_capacity_satisfiable",)
    assert "web-001.alpha.terminus" in refused.value.failures[0].reason


async def test_capacity_time_must_fit_the_deadline(build) -> None:
    capacity = [CapacitySpec("docker-a", configured_limit=1)]
    tight = await build(epochs=2, capacity=capacity, deadline_s=1000.0)
    roomy = await build(epochs=2, capacity=capacity, deadline_s=4000.0)  # 2 x 1800 s serially
    for h in (tight, roomy):
        h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await tight.driver.start()
    await roomy.driver.start()

    assert refused.value.failed_conditions == ("carrier_capacity_satisfiable",)
    assert "deadline" in refused.value.failures[0].reason
    assert roomy.driver.state is BatchState.RUNNING


def baseline_profile() -> Any:
    return MockSandboxProvider("docker-a", {}, DictSecretResolver()).isolation_profile()


def with_waiver(**changes: Any) -> Any:
    profile = baseline_profile()
    entry = InvariantEntry(
        how="waived",
        waived=True,
        reason="compensating canary",
        approved_by="lead",
        expires="2027-03-01",
    )
    return replace(profile, invariants={**profile.invariants, "I2": replace(entry, **changes)})


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (with_waiver(expires="2026-01-01"), "expired"),
        (with_waiver(expires="soon"), "bad expiry"),
        (with_waiver(reason=None), "no reason"),
        (
            replace(
                baseline_profile(),
                invariants={k: v for k, v in baseline_profile().invariants.items() if k != "I16"},
            ),
            "lacks I16",
        ),
    ],
)
async def test_invalid_isolation_profiles_block_running(build, profile: Any, message: str) -> None:
    h = await build(profiles={"docker-a": profile})
    h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await h.driver.start()

    assert refused.value.failed_conditions == ("isolation_profiles_valid",)
    assert message in refused.value.failures[0].reason


async def test_unexpired_waiver_and_complete_profile_pass(build) -> None:
    h = await build(profiles={"docker-a": with_waiver()})
    h.driver.freeze(lint_passed=True, budget_approved=True)

    await h.driver.start()

    assert h.driver.state is BatchState.RUNNING


async def test_profile_below_the_required_isolation_blocks_running(build, task_factory) -> None:
    weak_profile = replace(baseline_profile(), declared_isolation=Isolation.L1)
    h = await build(
        provider_caps=replace(default_capabilities(), isolation=Isolation.L3),
        tasks=[task_factory("web-001", min_isolation=Isolation.L3)],
        profiles={"docker-a": weak_profile},
    )
    h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await h.driver.start()

    assert refused.value.failed_conditions == ("isolation_profiles_valid",)
    assert "declared L1 < L3" in refused.value.failures[0].reason


async def test_missing_profile_blocks_running(build) -> None:
    h = await build(profiles={"someone-else": baseline_profile()})
    h.driver.freeze(lint_passed=True, budget_approved=True)

    with pytest.raises(BatchGuardFailed) as refused:
        await h.driver.start()

    assert refused.value.failed_conditions == ("isolation_profiles_valid",)
    assert "no isolation profile" in refused.value.failures[0].reason
