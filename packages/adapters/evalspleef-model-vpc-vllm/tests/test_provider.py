from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from evalspleef_model_vpc_vllm.clock import SteppingClock
from evalspleef_model_vpc_vllm.errors import (
    CredentialRejected,
    DeploymentNotFound,
    RequestRejected,
)
from evalspleef_model_vpc_vllm.k8s import InMemoryKubeApi
from evalspleef_model_vpc_vllm.provider import VpcVllmProvider

from evalspleef.spi.errors import EndpointUnavailable
from evalspleef.spi.model import (
    DEPLOYMENT_ID_KEY,
    DeploymentSpec,
    EndpointState,
    HFSource,
    ModelSpec,
    VendorSource,
)
from evalspleef.testing.secrets import DictSecretResolver

REVISION = "0" * 40
IMAGE = "vllm/vllm-openai@sha256:" + "b" * 64
HF_TOKEN_REF = "vault://hf/test-token"
HF_TOKEN_VALUE = "hf_secret_12345"


def _hf_spec(
    backend: str = "k8s",
    params: dict[str, Any] | None = None,
) -> ModelSpec:
    p = {"backend": backend, "image": IMAGE, "gpu_count": 1}
    if params:
        p.update(params)
    return ModelSpec(
        slug="test-qwen",
        spec_version=1,
        family="qwen",
        roles=frozenset({"target"}),
        source=HFSource(
            repo="Qwen/Qwen2.5-7B-Instruct",
            revision=REVISION,
            access="public",
            token_ref=HF_TOKEN_REF,
        ),
        deployment=DeploymentSpec(
            adapter="vpc-vllm",
            account="test-acc",
            params=p,
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="vpc-vllm",
        license="apache-2.0",
        owner="evalspleef",
    )


def _secrets() -> DictSecretResolver:
    return DictSecretResolver({HF_TOKEN_REF: HF_TOKEN_VALUE})


@pytest.mark.asyncio
async def test_validate() -> None:
    provider = VpcVllmProvider("test", {"image": IMAGE}, _secrets())

    # Valid spec
    valid_spec = _hf_spec()
    report = await provider.validate(valid_spec)
    assert report.ok

    # Invalid non-HF source
    non_hf = ModelSpec(
        slug="vendor-model",
        spec_version=1,
        family="generic",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="openai", model_id="gpt-4o"),
        deployment=DeploymentSpec(adapter="vpc-vllm", account="test"),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="ref",
        license="prop",
        owner="test",
    )
    report_non_hf = await provider.validate(non_hf)
    assert not report_non_hf.ok

    # Unpinned revision
    bad_rev_spec = ModelSpec(
        slug="bad-rev",
        spec_version=1,
        family="qwen",
        roles=frozenset({"target"}),
        source=HFSource(repo="Qwen/Qwen2.5-7B", revision="main", access="public"),
        deployment=DeploymentSpec(adapter="vpc-vllm", account="test"),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="ref",
        license="apache-2.0",
        owner="test",
    )
    report_bad_rev = await provider.validate(bad_rev_spec)
    assert not report_bad_rev.ok

    # Unpinned image
    bad_image_spec = _hf_spec(params={"image": "vllm/vllm-openai:latest"})
    report_bad_image = await provider.validate(bad_image_spec)
    assert not report_bad_image.ok


@pytest.mark.asyncio
async def test_plan() -> None:
    provider = VpcVllmProvider("test", {}, _secrets())
    spec = _hf_spec(params={"gpu_count": 2, "min_replicas": 1, "max_replicas": 3})
    plan = await provider.plan(spec)

    assert plan.gpu_count == 2
    assert plan.replicas_min == 1
    assert plan.replicas_max == 3
    assert plan.max_concurrency == 16


@pytest.mark.asyncio
async def test_deploy_k8s_backend() -> None:
    kube_api = InMemoryKubeApi()
    provider = VpcVllmProvider("test", {"image": IMAGE}, _secrets(), kube_api=kube_api)
    spec = _hf_spec(backend="k8s")
    plan = await provider.plan(spec)

    handle = await provider.deploy(spec, plan)

    assert handle.state == EndpointState.READY
    assert handle.base_url is not None
    assert handle.base_url.startswith("http://vllm-ep-test-qwen")
    assert handle.base_url.endswith(":8000/v1")
    assert handle.external_ids["backend"] == "k8s"

    # Upstream check
    route = provider.upstream(handle)
    assert route.api_base == handle.base_url
    assert route.model == spec.slug

    # Verify secret was injected into manifest but is not exposed in handle repr
    dep_name = handle.external_ids["name"]
    assert handle.external_ids[DEPLOYMENT_ID_KEY] == f"default/{dep_name}"
    dep = await kube_api.get_deployment("default", dep_name)
    assert dep is not None
    container = dep["spec"]["template"]["spec"]["containers"][0]
    envs = {e["name"]: e["value"] for e in container["env"]}
    assert envs["HUGGING_FACE_HUB_TOKEN"] == HF_TOKEN_VALUE
    assert HF_TOKEN_VALUE not in repr(handle)

    # Scale check
    await provider.scale(handle, 2, 2)
    dep_scaled = await kube_api.get_deployment("default", dep_name)
    assert dep_scaled is not None
    assert dep_scaled["spec"]["replicas"] == 2

    # Status check
    state = await provider.status(handle)
    assert state == EndpointState.READY

    # Teardown sleep
    await provider.teardown(handle, "sleep")
    assert handle.state == EndpointState.SLEEPING
    assert await provider.status(handle) == EndpointState.SLEEPING

    # Teardown terminate
    await provider.teardown(handle, "terminate")
    assert handle.state == EndpointState.RETIRED
    assert await provider.status(handle) == EndpointState.RETIRED


@pytest.mark.asyncio
async def test_deploy_gpu_instance_backend() -> None:
    instances: dict[str, Any] = {}

    def handle_req(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/gpu-instance/openapi/v1/gpu/instance/create":
            inst_id = "inst-gpu-42"
            instances[inst_id] = {
                "instance_id": inst_id,
                "status": "running",
                "ip": "192.168.1.100",
            }
            return httpx.Response(200, json={"instance_id": inst_id})
        if path.startswith("/gpu-instance/openapi/v1/gpu/instance/get"):
            inst_id = request.url.params.get("instance_id") or path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"data": instances[inst_id]})
        if path == "/gpu-instance/openapi/v1/gpu/instance/stop":
            instances[body["instance_id"]]["status"] = "stopped"
            return httpx.Response(200, json={})
        if path == "/gpu-instance/openapi/v1/gpu/instance/delete":
            instances[body["instance_id"]]["status"] = "deleted"
            return httpx.Response(200, json={})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle_req)
    provider = VpcVllmProvider(
        "test",
        {"image": IMAGE, "backend": "gpu_instance"},
        _secrets(),
        gpu_transport=transport,
    )
    spec = _hf_spec(backend="gpu_instance")
    plan = await provider.plan(spec)

    handle = await provider.deploy(spec, plan)

    assert handle.state == EndpointState.READY
    assert handle.base_url == "http://192.168.1.100:8000/v1"
    assert handle.external_ids["backend"] == "gpu_instance"
    assert handle.external_ids[DEPLOYMENT_ID_KEY] == handle.external_ids["instance_id"]

    state = await provider.status(handle)
    assert state == EndpointState.READY

    await provider.teardown(handle, "sleep")
    assert handle.state == EndpointState.SLEEPING

    await provider.teardown(handle, "terminate")
    assert handle.state == EndpointState.RETIRED


@pytest.mark.asyncio
async def test_wake_outcomes_and_error_mapping() -> None:
    clock = SteppingClock()
    injected_status = 200

    def chat_mock(request: httpx.Request) -> httpx.Response:
        if injected_status == 200:
            return httpx.Response(
                200, json={"model": "test-qwen", "choices": [{"message": {"content": "ok"}}]}
            )
        if injected_status == 401:
            return httpx.Response(401, json={"message": "unauthorized"})
        if injected_status == 404:
            return httpx.Response(404, json={"message": "not found"})
        if injected_status == 422:
            return httpx.Response(422, json={"message": "unprocessable"})
        if injected_status == 429:
            return httpx.Response(429, json={"message": "rate limit"})
        if injected_status == 503:
            return httpx.Response(503, json={"message": "service unavailable"})
        return httpx.Response(500, json={"message": "internal error"})

    probe_transport = httpx.MockTransport(chat_mock)
    provider = VpcVllmProvider(
        "test",
        {"image": IMAGE},
        _secrets(),
        clock=clock,
        kube_api=InMemoryKubeApi(),
        probe_transport=probe_transport,
    )
    spec = _hf_spec()
    plan = await provider.plan(spec)
    handle = await provider.deploy(spec, plan)

    # 200 -> WARM
    injected_status = 200
    state = await provider.wake(handle, timeout_s=30)
    assert state == EndpointState.WARM

    # 401 -> CredentialRejected
    injected_status = 401
    with pytest.raises(CredentialRejected) as exc:
        await provider.wake(handle, timeout_s=30)
    assert exc.value.retryable is False

    # 404 -> DeploymentNotFound
    injected_status = 404
    with pytest.raises(DeploymentNotFound) as exc:
        await provider.wake(handle, timeout_s=30)
    assert exc.value.retryable is False

    # 422 -> RequestRejected
    injected_status = 422
    with pytest.raises(RequestRejected) as exc:
        await provider.wake(handle, timeout_s=30)
    assert exc.value.retryable is False

    # 429 -> EndpointUnavailable (retryable=True)
    injected_status = 429
    with pytest.raises(EndpointUnavailable) as exc:
        await provider.wake(handle, timeout_s=30)
    assert exc.value.retryable is True

    # 503 -> EndpointUnavailable (retryable=True)
    injected_status = 503
    with pytest.raises(EndpointUnavailable) as exc:
        await provider.wake(handle, timeout_s=30)
    assert exc.value.retryable is True
