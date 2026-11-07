from __future__ import annotations

import pytest
from evalspleef_model_vpc_vllm.k8s import (
    InMemoryKubeApi,
    KubeOrchestrator,
    build_deployment_manifest,
    build_service_manifest,
    map_deployment_status,
)

from evalspleef.spi.model import EndpointState


def test_build_deployment_manifest() -> None:
    manifest = build_deployment_manifest(
        name="vllm-test",
        namespace="models",
        image="vllm/vllm-openai@sha256:" + "0" * 64,
        args=["--model", "test/repo"],
        replicas=2,
        gpu_count=4,
        hf_token="hf_secret_token",
        extra_env={"CUSTOM_FLAG": "1"},
        port=8000,
    )

    assert manifest["apiVersion"] == "apps/v1"
    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"]["name"] == "vllm-test"
    assert manifest["metadata"]["namespace"] == "models"
    assert manifest["spec"]["replicas"] == 2

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "vllm/vllm-openai@sha256:" + "0" * 64
    assert container["args"] == ["--model", "test/repo"]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "4"

    env_dict = {item["name"]: item["value"] for item in container["env"]}
    assert env_dict["HUGGING_FACE_HUB_TOKEN"] == "hf_secret_token"
    assert env_dict["CUSTOM_FLAG"] == "1"


def test_build_service_manifest() -> None:
    manifest = build_service_manifest(
        name="vllm-svc",
        namespace="models",
        port=8000,
    )

    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "Service"
    assert manifest["metadata"]["name"] == "vllm-svc"
    assert manifest["spec"]["type"] == "ClusterIP"
    assert manifest["spec"]["ports"][0]["port"] == 8000


@pytest.mark.asyncio
async def test_in_memory_kube_api() -> None:
    api = InMemoryKubeApi()
    dep_manifest = build_deployment_manifest(
        name="test-dep",
        namespace="default",
        image="test-image",
        args=[],
        replicas=1,
    )
    svc_manifest = build_service_manifest(name="test-dep", namespace="default")

    await api.create_deployment("default", dep_manifest)
    await api.create_service("default", svc_manifest)

    dep = await api.get_deployment("default", "test-dep")
    assert dep is not None
    assert dep["metadata"]["name"] == "test-dep"

    svc = await api.get_service("default", "test-dep")
    assert svc is not None

    await api.patch_deployment_scale("default", "test-dep", 3)
    scaled = await api.get_deployment("default", "test-dep")
    assert scaled is not None
    assert scaled["spec"]["replicas"] == 3

    await api.delete_deployment("default", "test-dep")
    await api.delete_service("default", "test-dep")

    assert await api.get_deployment("default", "test-dep") is None
    assert await api.get_service("default", "test-dep") is None


def test_map_deployment_status() -> None:
    assert map_deployment_status(None) == EndpointState.RETIRED

    dep_sleeping = {"spec": {"replicas": 0}, "status": {"replicas": 0}}
    assert map_deployment_status(dep_sleeping) == EndpointState.SLEEPING

    dep_ready = {
        "spec": {"replicas": 1},
        "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1},
    }
    assert map_deployment_status(dep_ready) == EndpointState.READY

    dep_deploying = {
        "spec": {"replicas": 2},
        "status": {"replicas": 2, "readyReplicas": 1, "availableReplicas": 1},
    }
    assert map_deployment_status(dep_deploying) == EndpointState.DEPLOYING

    dep_failed = {
        "spec": {"replicas": 1},
        "status": {
            "replicas": 1,
            "conditions": [{"type": "ReplicaFailure", "status": "True"}],
        },
    }
    assert map_deployment_status(dep_failed) == EndpointState.FAILED


@pytest.mark.asyncio
async def test_kube_orchestrator_lifecycle() -> None:
    api = InMemoryKubeApi()
    orch = KubeOrchestrator(api, namespace="test-ns")

    name, url = await orch.deploy(
        endpoint_id="ep_model_123",
        image="vllm:pinned",
        args=["--arg1"],
        replicas=1,
        gpu_count=1,
        hf_token="token",
    )
    assert name.startswith("vllm-ep-model-123")
    assert url == f"http://{name}:8000/v1"

    state = await orch.status(name)
    assert state == EndpointState.READY

    await orch.scale(name, 2, 2)
    dep = await api.get_deployment("test-ns", name)
    assert dep is not None
    assert dep["spec"]["replicas"] == 2

    # Teardown sleep
    sleep_state = await orch.teardown(name, "sleep")
    assert sleep_state == EndpointState.SLEEPING
    assert await orch.status(name) == EndpointState.SLEEPING

    # Teardown terminate
    term_state = await orch.teardown(name, "terminate")
    assert term_state == EndpointState.RETIRED
    assert await orch.status(name) == EndpointState.RETIRED
