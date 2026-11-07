from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from evalspleef_model_novita.conformance import (
    API_KEY,
    API_KEY_REF,
    HF_TOKEN_REF,
    IMAGE,
    FakeHFChecker,
    FakeNovitaGpuApi,
    FakeNovitaInferenceServer,
    SteppingClock,
)
from evalspleef_model_novita.errors import (
    AccessForbidden,
    CredentialRejected,
    DeploymentNotFound,
    RequestRejected,
)
from evalspleef_model_novita.serverless_gpu import NovitaServerlessGpuProvider, map_status

from evalspleef.spi.errors import EndpointUnavailable
from evalspleef.spi.model import EndpointHandle, EndpointState, ManagementMode, ModelSpec
from evalspleef.testing.secrets import DictSecretResolver

SpecFactory = Callable[..., ModelSpec]
SERVED = "qwen3-32b"
SHA = "1a2b3c4d" * 5


@pytest.fixture
def api(inference_server: FakeNovitaInferenceServer) -> FakeNovitaGpuApi:
    return FakeNovitaGpuApi(
        api_key=API_KEY, inference_url=inference_server.base_url.removesuffix("/v1")
    )


@pytest.fixture
def provider(
    api: FakeNovitaGpuApi,
    clock: SteppingClock,
    secrets: DictSecretResolver,
    checker: FakeHFChecker,
) -> NovitaServerlessGpuProvider:
    instance = NovitaServerlessGpuProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, "image": IMAGE},
        secrets,
        clock=clock,
        api_transport=api.transport(),
    )
    instance.hf_checker = checker
    return instance


async def deployed(provider: NovitaServerlessGpuProvider, spec: ModelSpec) -> EndpointHandle:
    return await provider.deploy(spec, await provider.plan(spec))


def test_is_api_mode_and_hf_checker_defaults_to_none() -> None:
    assert NovitaServerlessGpuProvider.management_mode is ManagementMode.API
    assert NovitaServerlessGpuProvider.hf_checker is None


# -- validate -----------------------------------------------------------------------------------


async def test_validate_accepts_a_pinned_public_model(
    provider: NovitaServerlessGpuProvider, make_spec: SpecFactory
) -> None:
    report = await provider.validate(make_spec())
    assert report.ok, report.checks


async def test_validate_without_hf_checker_reports_the_named_check(
    provider: NovitaServerlessGpuProvider, make_spec: SpecFactory
) -> None:
    provider.hf_checker = None
    report = await provider.validate(make_spec())
    assert ("hf_checker_missing", False) in [(cid, ok) for cid, ok, _ in report.checks]


async def test_validate_requires_a_digest_pinned_image_and_a_resolvable_hf_token(
    api: FakeNovitaGpuApi,
    clock: SteppingClock,
    checker: FakeHFChecker,
    make_spec: SpecFactory,
) -> None:
    loose = NovitaServerlessGpuProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, "image": "vllm/vllm-openai:latest"},
        DictSecretResolver({API_KEY_REF: API_KEY}),
        clock=clock,
        api_transport=api.transport(),
    )
    loose.hf_checker = checker
    spec = make_spec(
        access="private", token_ref=HF_TOKEN_REF, repo="evalspleef-conformance/public-model"
    )
    failed = {cid for cid, ok, _ in (await loose.validate(spec)).checks if not ok}
    assert failed == {"image_digest_pinned", "hf_token"}


# -- deploy -------------------------------------------------------------------------------------


async def test_deploy_posts_the_documented_create_request(
    provider: NovitaServerlessGpuProvider,
    api: FakeNovitaGpuApi,
    make_spec: SpecFactory,
) -> None:
    spec = make_spec(
        mirror_repo="org/mirror-qwen3-32b-1a2b3c4",
        min_replicas=2,
        max_replicas=3,
        max_concurrency=8,
        gpu_count=2,
    )
    handle = await deployed(provider, spec)
    method, path, body = api.calls[0]
    assert (method, path) == ("POST", "/gpus/v2/endpoints")
    assert body is not None
    assert body["type"] == "sync"
    assert body["product_id"] == "prod-h100"
    assert body["image"] == IMAGE
    assert body["worker_config"] == {
        "min_replicas": 2,
        "max_replicas": 3,
        "max_concurrent_per_worker": 8,
        "gpu_num": 2,
        "idle_timeout": 300,
        "request_timeout": 600,
    }
    assert body["health_check"] == {"path": "/health", "port": 8000}
    assert body["ports"] == [8000]
    command = body["command"].split()
    assert command[command.index("--model") + 1] == "org/mirror-qwen3-32b-1a2b3c4"
    assert command[command.index("--revision") + 1] == SHA
    assert command[command.index("--served-model-name") + 1] == SERVED
    assert command[command.index("--tensor-parallel-size") + 1] == "2"
    assert command[command.index("--tool-call-parser") + 1] == "hermes"
    assert handle.state is EndpointState.READY
    assert handle.upstream_model == SERVED
    assert handle.external_ids["novita_endpoint_id"] in api.endpoints
    assert handle.observed["revision"] == SHA
    assert handle.observed["image"] == IMAGE


async def test_deploy_sends_the_hf_token_but_never_records_or_logs_it(
    provider: NovitaServerlessGpuProvider,
    api: FakeNovitaGpuApi,
    make_spec: SpecFactory,
    secrets: DictSecretResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hf_secret = secrets.resolve(HF_TOKEN_REF)
    caplog.set_level(logging.DEBUG)
    spec = make_spec(access="private", token_ref=HF_TOKEN_REF)
    handle = await deployed(provider, spec)
    body: Any = api.calls[0][2]
    assert body["envs"] == [{"key": "HUGGING_FACE_HUB_TOKEN", "value": hf_secret}]
    assert hf_secret not in repr(handle.observed)
    assert hf_secret not in caplog.text
    assert handle.observed["deploy_request"]["envs"] == [
        {"key": "HUGGING_FACE_HUB_TOKEN", "value": "***"}
    ]


async def test_vendor_error_text_is_scrubbed_of_the_token(
    clock: SteppingClock, secrets: DictSecretResolver, make_spec: SpecFactory
) -> None:
    hf_secret = secrets.resolve(HF_TOKEN_REF)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": f"bad env value {hf_secret}"})

    provider = NovitaServerlessGpuProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, "image": IMAGE},
        secrets,
        clock=clock,
        api_transport=httpx.MockTransport(handler),
    )
    spec = make_spec(access="private", token_ref=HF_TOKEN_REF)
    with pytest.raises(RequestRejected) as error:
        await deployed(provider, spec)
    assert hf_secret not in str(error.value)
    assert "***" in str(error.value)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, CredentialRejected),
        (403, AccessForbidden),
        (404, DeploymentNotFound),
        (400, RequestRejected),
        (429, EndpointUnavailable),
        (503, EndpointUnavailable),
    ],
)
async def test_rest_failures_map_to_distinct_errors(
    clock: SteppingClock,
    secrets: DictSecretResolver,
    make_spec: SpecFactory,
    status: int,
    error_type: type[Exception],
) -> None:
    provider = NovitaServerlessGpuProvider(
        "acme",
        {"api_key_ref": API_KEY_REF, "image": IMAGE},
        secrets,
        clock=clock,
        api_transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"message": "x"})),
    )
    with pytest.raises(error_type) as error:
        await deployed(provider, make_spec())
    assert type(error.value) is error_type


async def test_deploy_that_is_still_starting_stays_deploying(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    api.status_value = "creating"
    handle = await deployed(provider, make_spec())
    assert handle.state is EndpointState.DEPLOYING
    api.status_value = "running"
    assert await provider.status(handle) is EndpointState.READY


# -- status mapping -------------------------------------------------------------------------------

READY_WORKER = {"status": {"status": "ready"}, "health": True, "drain": False}


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        ({"status": {"status": "running"}, "workers": [READY_WORKER]}, EndpointState.READY),
        ({"status": {"status": "serving"}, "workers": [READY_WORKER]}, EndpointState.READY),
        ({"status": {"status": "creating"}}, EndpointState.DEPLOYING),
        ({"status": {"status": "pulling"}}, EndpointState.DEPLOYING),
        ({"status": {"status": "failed", "error": "OOM"}}, EndpointState.FAILED),
        ({"status": {"status": "weird", "error": "ImagePullBackOff"}}, EndpointState.FAILED),
        ({"status": {"status": "terminated"}}, EndpointState.RETIRED),
        ({"status": {"status": "sleeping"}}, EndpointState.SLEEPING),
        (
            {"status": {"status": "running"}, "workers": [], "worker_config": {"min_replicas": 0}},
            EndpointState.SLEEPING,
        ),
        (
            {"status": {"status": "running"}, "workers": [], "worker_config": {"min_replicas": 2}},
            EndpointState.DEPLOYING,
        ),
        (
            {
                "status": {"status": "running"},
                "workers": [{**READY_WORKER, "health": False}],
                "worker_config": {"min_replicas": 1},
            },
            EndpointState.DEPLOYING,
        ),
    ],
)
def test_map_status(payload: dict[str, Any], state: EndpointState) -> None:
    assert map_status(payload) == (state, None)


def test_unknown_status_degrades_and_is_reported() -> None:
    assert map_status({"status": {"status": "quantum"}}) == (EndpointState.DEGRADED, "quantum")


async def test_status_of_an_endpoint_deleted_elsewhere_is_failed(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    api.endpoints.clear()
    assert await provider.status(handle) is EndpointState.FAILED


# -- scale / teardown -----------------------------------------------------------------------------


async def test_scale_puts_the_merged_worker_config(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec(max_concurrency=8))
    await provider.scale(handle, 2, 4)
    method, path, body = api.calls[-1]
    assert method == "PUT"
    assert path == f"/gpus/v2/endpoints/{handle.external_ids['novita_endpoint_id']}"
    assert body is not None
    assert body["worker_config"]["min_replicas"] == 2
    assert body["worker_config"]["max_replicas"] == 4
    assert body["worker_config"]["max_concurrent_per_worker"] == 8  # kept from the GET
    assert body["worker_config"]["request_timeout"] == 600


async def test_teardown_sleep_scales_to_zero_and_terminate_deletes(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec(max_replicas=3))
    remote = handle.external_ids["novita_endpoint_id"]
    await provider.teardown(handle, "sleep")
    assert api.endpoints[remote]["worker_config"]["min_replicas"] == 0
    assert api.endpoints[remote]["worker_config"]["max_replicas"] == 3
    assert handle.state is EndpointState.SLEEPING
    await provider.teardown(handle, "terminate")
    assert remote not in api.endpoints
    assert api.calls[-1][0] == "DELETE"
    assert handle.state is EndpointState.RETIRED
    assert await provider.status(handle) is EndpointState.RETIRED


async def test_terminate_of_an_already_deleted_endpoint_is_idempotent(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    api.endpoints.clear()
    await provider.teardown(handle, "terminate")
    assert handle.state is EndpointState.RETIRED


# -- upstream / wake ------------------------------------------------------------------------------


async def test_upstream_is_the_endpoint_url_plus_v1(
    provider: NovitaServerlessGpuProvider,
    api: FakeNovitaGpuApi,
    inference_server: FakeNovitaInferenceServer,
    make_spec: SpecFactory,
) -> None:
    handle = await deployed(provider, make_spec())
    route = provider.upstream(handle)
    assert route.api_base == f"{api.inference_url}/v1" == handle.base_url
    assert route.model == SERVED
    assert route.api_key_ref == API_KEY_REF


async def test_upstream_before_the_url_exists_is_unavailable(
    provider: NovitaServerlessGpuProvider, api: FakeNovitaGpuApi, make_spec: SpecFactory
) -> None:
    api.inference_url = ""
    handle = await deployed(provider, make_spec())
    assert handle.base_url is None
    with pytest.raises(EndpointUnavailable):
        provider.upstream(handle)


async def test_wake_waits_out_a_cold_start_and_ends_warm(
    provider: NovitaServerlessGpuProvider,
    inference_server: FakeNovitaInferenceServer,
    make_spec: SpecFactory,
) -> None:
    handle = await deployed(provider, make_spec())
    inference_server.go_to_sleep()
    assert await provider.wake(handle, timeout_s=600) is EndpointState.WARM
    assert handle.observed["last_wake_s"] >= inference_server.cold_start_s
    assert inference_server.requests, "the fake endpoint saw the wake probe"
