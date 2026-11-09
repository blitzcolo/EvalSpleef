from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from evalspleef_model_novita.conformance import (
    API_KEY_REF,
    FakeHFChecker,
    FakeNovitaInferenceServer,
    SteppingClock,
)
from evalspleef_model_novita.dedicated import COLD_START_WINDOW_S, NovitaDedicatedProvider
from evalspleef_model_novita.errors import (
    AccessForbidden,
    CredentialRejected,
    DeploymentNotFound,
    InvalidBaseUrl,
    ModelNameMismatch,
    PlanInputMissing,
    RequestRejected,
)

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable, SpleefError
from evalspleef.spi.model import (
    DEPLOYMENT_ID_KEY,
    EndpointHandle,
    EndpointState,
    ManagementMode,
    ModelSpec,
)
from evalspleef.testing.secrets import DictSecretResolver

SpecFactory = Callable[..., ModelSpec]
MODEL = "org/model"
BASE = "https://abc123.novita.ai/v1"
NOT_READY = {"error": {"message": "model is loading: service not ready", "code": "x"}}
OPERATOR = {"base_url": BASE, "model": MODEL, "deployment_id": "dep-1"}


def ok(model: str = MODEL) -> httpx.Response:
    return httpx.Response(200, json={"model": model, "choices": []})


def provider_for(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: SteppingClock,
    secrets: DictSecretResolver,
    checker: FakeHFChecker | None = None,
    **config: object,
) -> NovitaDedicatedProvider:
    provider = NovitaDedicatedProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, **config},
        secrets,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )
    provider.hf_checker = checker
    return provider


async def attached(provider: NovitaDedicatedProvider, spec: ModelSpec) -> EndpointHandle:
    handle = await provider.deploy(spec, await provider.plan(spec))
    return await provider.attach(handle, OPERATOR)


def test_is_assisted_and_hf_checker_defaults_to_none() -> None:
    assert NovitaDedicatedProvider.management_mode is ManagementMode.ASSISTED
    assert NovitaDedicatedProvider.hf_checker is None


# -- validate -------------------------------------------------------------------------------


async def test_validate_without_hf_checker_fails_with_the_named_check(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets, checker=None)
    report = await provider.validate(make_spec())
    assert not report.ok
    assert ("hf_checker_missing", False) in [(cid, passed) for cid, passed, _ in report.checks]


async def test_validate_passes_for_a_readable_safetensors_repo(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    checker: FakeHFChecker,
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets, checker)
    report = await provider.validate(make_spec(weight_files=["model-00001.safetensors"]))
    assert report.ok, report.checks


async def test_validate_rejects_bin_weights_and_unresolvable_key(
    make_spec: SpecFactory, clock: SteppingClock, checker: FakeHFChecker
) -> None:
    provider = provider_for(lambda r: ok(), clock, DictSecretResolver({}), checker)
    report = await provider.validate(make_spec(weight_files=["pytorch_model.bin"]))
    failed = {cid for cid, passed, _ in report.checks if not passed}
    assert failed == {"novita_key", "safetensors_weights"}


async def test_validate_surfaces_hf_preflight_failures(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    checker: FakeHFChecker,
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets, checker)
    spec = make_spec(access="gated", repo="evalspleef-conformance/gated-model")
    report = await provider.validate(spec)
    assert not report.ok
    assert any(cid.startswith("hf_") and not passed for cid, passed, _ in report.checks)


# -- plan / deploy ----------------------------------------------------------------------------


async def test_plan_sizes_gpu_replicas_and_cost(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec(peak_concurrency=40, mirror_repo="org/mirror-qwen3-32b-1a2b3c4")
    plan = await provider.plan(spec)
    # (16e9 weights + 16 GiB KV at 16 concurrent x 8192 tokens) x 1.2 = 37.1 GiB: only the H100 fits.
    assert plan.est_vram_gb == pytest.approx(37.1, abs=0.1)
    assert (plan.gpu_type, plan.gpu_count) == ("H100 SXM 80GB", 1)
    assert (plan.replicas_min, plan.replicas_max, plan.max_concurrency) == (3, 3, 16)
    assert plan.est_cost_per_hour_usd == pytest.approx(3 * 1.99)
    md = plan.operator_instructions_md or ""
    assert "org/mirror-qwen3-32b-1a2b3c4" in md
    endpoint_id = (await provider.deploy(spec, plan)).endpoint_id
    assert (
        f"spleef models attach {endpoint_id} --base-url <...> --model <...> --deployment-id <...>"
        in md
    )


async def test_plan_reads_weight_bytes_from_safetensors_sizes(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec(weight_bytes=None, safetensors_sizes={"a": 6_000_000_000, "b": 10_000_000_000})
    assert (await provider.plan(spec)).est_vram_gb == pytest.approx(37.1, abs=0.1)


async def test_plan_without_sizing_inputs_names_what_is_missing(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    with pytest.raises(PlanInputMissing) as arch:
        await provider.plan(make_spec(head_dim=None))
    assert arch.value.detail["missing"] == ["head_dim"]
    with pytest.raises(PlanInputMissing) as weights:
        await provider.plan(make_spec(weight_bytes=None))
    assert weights.value.detail["missing"] == ["weight_bytes"]


async def test_plan_takes_the_catalog_price_override(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets, gpu_prices={"H100 SXM 80GB": 2.5})
    assert (await provider.plan(make_spec())).est_cost_per_hour_usd == pytest.approx(2.5)


async def test_deploy_returns_an_awaiting_operator_handle_with_a_24h_ticket(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    assert handle.state is EndpointState.AWAITING_OPERATOR
    assert handle.base_url is None
    ticket = handle.observed["ticket"]
    assert ticket["expires_at"] - ticket["opened_at"] == 24 * 3600
    assert handle.observed["operator_instructions_md"] in ticket["text"]


async def test_an_untouched_ticket_expires_into_failed(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    assert await provider.status(handle) is EndpointState.AWAITING_OPERATOR
    clock.advance(24 * 3600 + 1)
    assert await provider.status(handle) is EndpointState.FAILED


# -- attach -----------------------------------------------------------------------------------


async def test_attach_sends_a_one_token_request_and_moves_to_verifying(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()

    provider = provider_for(handler, clock, secrets)
    handle = await attached(provider, make_spec())
    assert handle.state is EndpointState.VERIFYING
    assert (handle.base_url, handle.upstream_model) == (BASE, MODEL)
    assert handle.observed["deployment_id"] == "dep-1"
    assert handle.external_ids[DEPLOYMENT_ID_KEY] == "dep-1"
    (request,) = seen
    assert str(request.url) == f"{BASE}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-novita-conformance"
    body = request.read()
    assert b'"max_tokens":1' in body.replace(b" ", b"")


@pytest.mark.parametrize(
    ("base_url", "reason"),
    [
        ("http://abc123.novita.ai/v1", "base_url_not_https"),
        ("https://api.example.com/v1", "base_url_host"),
        ("https://novita.ai.evil.com/v1", "base_url_host"),
        ("https://user:pw@abc123.novita.ai/v1", "base_url_host"),
        ("http://127.0.0.1:9/v1", "base_url_not_https"),
    ],
)
async def test_attach_rejects_bad_base_urls_without_sending_anything(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    base_url: str,
    reason: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a rejected base_url")

    provider = provider_for(handler, clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(InvalidBaseUrl) as error:
        await provider.attach(handle, {**OPERATOR, "base_url": base_url})
    assert error.value.detail["reason"] == reason
    assert handle.state is EndpointState.AWAITING_OPERATOR
    assert handle.base_url is None


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, CredentialRejected),
        (403, AccessForbidden),
        (404, DeploymentNotFound),
        (422, RequestRejected),
    ],
)
async def test_attach_maps_each_http_failure_to_its_own_error(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    status: int,
    error_type: type[EndpointMismatch],
) -> None:
    provider = provider_for(lambda r: httpx.Response(status, text="no"), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(error_type) as error:
        await provider.attach(handle, OPERATOR)
    assert type(error.value) is error_type
    assert error.value.detail["http_status"] is None or error.value.detail["http_status"] == status
    assert handle.state is EndpointState.AWAITING_OPERATOR


async def test_attach_rejects_a_model_that_differs_from_the_registration(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(ModelNameMismatch) as error:
        await provider.attach(handle, {**OPERATOR, "model": "org/other"})
    assert error.value.detail["reason"] == "model_mismatch"


async def test_attach_rejects_an_endpoint_that_answers_as_another_model(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok("org/impostor"), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(ModelNameMismatch) as error:
        await provider.attach(handle, OPERATOR)
    assert error.value.detail["actual"] == "org/impostor"
    assert handle.state is EndpointState.AWAITING_OPERATOR


async def test_attach_to_a_cold_endpoint_is_a_retryable_error(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: httpx.Response(503, json=NOT_READY), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(EndpointUnavailable) as error:
        await provider.attach(handle, OPERATOR)
    assert error.value.retryable


# -- status -----------------------------------------------------------------------------------


async def test_status_success_is_warm(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    handle = await attached(provider, make_spec())
    assert await provider.status(handle) is EndpointState.WARM


async def test_status_slow_first_answer_is_a_recorded_cold_start(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    slow = False

    def handler(request: httpx.Request) -> httpx.Response:
        if slow:
            clock.advance(61)
        return ok()

    provider = provider_for(handler, clock, secrets)
    handle = await attached(provider, make_spec())
    slow = True
    assert await provider.status(handle) is EndpointState.SLEEPING
    assert handle.observed["first_cold_start_s"] == 61
    slow = False
    assert await provider.status(handle) is EndpointState.WARM


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(503, json=NOT_READY), EndpointState.SLEEPING),
        (httpx.Response(401, text=""), EndpointState.FAILED),
        (httpx.Response(403, text=""), EndpointState.FAILED),
        (httpx.Response(404, text=""), EndpointState.FAILED),
        (httpx.Response(500, text="boom"), EndpointState.DEGRADED),
        (ok("org/drifted"), EndpointState.DEGRADED),
    ],
)
async def test_status_maps_inference_answers(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    response: httpx.Response,
    expected: EndpointState,
) -> None:
    answer = ok()

    provider = provider_for(lambda r: answer, clock, secrets)
    handle = await attached(provider, make_spec())
    answer = response
    assert await provider.status(handle) is expected


async def test_status_read_timeout_is_a_cold_start(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    held = False

    def handler(request: httpx.Request) -> httpx.Response:
        if held:
            raise httpx.ReadTimeout("held", request=request)
        return ok()

    provider = provider_for(handler, clock, secrets)
    handle = await attached(provider, make_spec())
    held = True
    assert await provider.status(handle) is EndpointState.SLEEPING


# -- wake / cold start against the fake endpoint --------------------------------------------------


async def attach_to_fake(
    provider: NovitaDedicatedProvider, spec: ModelSpec, server: FakeNovitaInferenceServer
) -> EndpointHandle:
    handle = await provider.deploy(spec, await provider.plan(spec))
    return await provider.attach(
        handle, {"base_url": server.base_url, "model": MODEL, "deployment_id": "dep-1"}
    )


def fake_provider(clock: SteppingClock, secrets: DictSecretResolver) -> NovitaDedicatedProvider:
    return NovitaDedicatedProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, "allow_loopback_http": True},
        secrets,
        clock=clock,
    )


async def test_wake_waits_out_a_rejecting_cold_start(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    inference_server: FakeNovitaInferenceServer,
) -> None:
    provider = fake_provider(clock, secrets)
    handle = await attach_to_fake(provider, make_spec(), inference_server)
    inference_server.go_to_sleep()
    assert await provider.status(handle) is EndpointState.SLEEPING
    state = await provider.wake(handle, timeout_s=600)
    assert state is EndpointState.WARM
    assert inference_server.not_ready_count >= 2
    assert handle.observed["last_wake_s"] >= inference_server.cold_start_s
    assert handle.observed["first_cold_start_s"] >= inference_server.cold_start_s


async def test_wake_gives_up_at_the_deadline_and_stays_sleeping(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    inference_server: FakeNovitaInferenceServer,
) -> None:
    provider = fake_provider(clock, secrets)
    handle = await attach_to_fake(provider, make_spec(), inference_server)
    inference_server.go_to_sleep()
    started = clock.now()
    assert await provider.wake(handle, timeout_s=60) is EndpointState.SLEEPING
    assert handle.state is EndpointState.SLEEPING
    assert clock.now() - started <= 60


async def test_a_held_first_request_is_seen_as_a_slow_answer(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    inference_server: FakeNovitaInferenceServer,
) -> None:
    provider = fake_provider(clock, secrets)
    handle = await attach_to_fake(provider, make_spec(), inference_server)
    inference_server.hold = True
    inference_server.go_to_sleep()
    assert await provider.status(handle) is EndpointState.SLEEPING
    assert handle.observed["first_cold_start_s"] == inference_server.cold_start_s
    assert await provider.status(handle) is EndpointState.WARM


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (401, CredentialRejected, False),
        (403, AccessForbidden, False),
        (404, DeploymentNotFound, False),
        (429, EndpointUnavailable, True),
        (500, EndpointUnavailable, True),
    ],
)
async def test_wake_raises_the_distinct_error_for_a_hard_failure(
    make_spec: SpecFactory,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    inference_server: FakeNovitaInferenceServer,
    status: int,
    error_type: type[SpleefError],
    retryable: bool,
) -> None:
    provider = fake_provider(clock, secrets)
    handle = await attach_to_fake(provider, make_spec(), inference_server)
    inference_server.inject_status(status)
    with pytest.raises(error_type) as error:
        await provider.wake(handle, timeout_s=600)
    assert type(error.value) is error_type
    assert error.value.retryable is retryable


# -- scale / teardown tickets -----------------------------------------------------------------------


async def test_terminate_ticket_is_confirmed_only_once_the_deployment_is_gone(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    answer = ok()
    provider = provider_for(lambda r: answer, clock, secrets)
    handle = await attached(provider, make_spec())
    await provider.teardown(handle, "terminate")
    assert handle.observed["pending_ticket"]["kind"] == "teardown"
    assert handle.state is EndpointState.VERIFYING  # nothing happens before the operator acts
    with pytest.raises(EndpointUnavailable):
        await provider.confirm_ticket(handle)  # still answering 200
    assert "pending_ticket" in handle.observed
    answer = httpx.Response(404, text="gone")
    assert await provider.confirm_ticket(handle) is EndpointState.RETIRED
    assert "pending_ticket" not in handle.observed
    assert await provider.status(handle) is EndpointState.RETIRED


async def test_scale_ticket_is_verified_with_a_health_request(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok()

    provider = provider_for(handler, clock, secrets)
    handle = await attached(provider, make_spec())
    sent = len(requests)
    await provider.scale(handle, 3, 3)
    assert len(requests) == sent  # opening the ticket sends nothing
    assert await provider.confirm_ticket(handle) is EndpointState.WARM
    assert len(requests) == sent + 1
    assert handle.observed["min_replicas"] == 3


async def test_scale_to_zero_and_sleep_are_not_probed_because_that_would_wake_the_endpoint(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok()

    provider = provider_for(handler, clock, secrets)
    handle = await attached(provider, make_spec())
    sent = len(requests)
    await provider.teardown(handle, "sleep")
    assert await provider.confirm_ticket(handle) is EndpointState.SLEEPING
    assert len(requests) == sent


async def test_confirming_without_a_ticket_is_an_error(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    handle = await attached(provider, make_spec())
    with pytest.raises(EndpointMismatch):
        await provider.confirm_ticket(handle)


async def test_teardown_before_any_deployment_retires_directly(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    await provider.teardown(handle, "terminate")
    assert handle.state is EndpointState.RETIRED


# -- upstream ---------------------------------------------------------------------------------------


async def test_upstream_route_carries_the_cold_start_window(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    handle = await attached(provider, make_spec())
    route = provider.upstream(handle)
    assert (route.api_base, route.model) == (BASE, MODEL)
    assert route.api_key_ref == API_KEY_REF
    assert route.cold_start_window_s == COLD_START_WINDOW_S == 480
    assert route.protocol == "chat_completions"


async def test_upstream_before_attach_is_unavailable(
    make_spec: SpecFactory, clock: SteppingClock, secrets: DictSecretResolver
) -> None:
    provider = provider_for(lambda r: ok(), clock, secrets)
    spec = make_spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    with pytest.raises(EndpointUnavailable):
        provider.upstream(handle)
