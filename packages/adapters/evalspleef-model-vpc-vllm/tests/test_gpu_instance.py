from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from evalspleef_model_vpc_vllm.errors import (
    AccessForbidden,
    CredentialRejected,
    DeploymentNotFound,
)
from evalspleef_model_vpc_vllm.gpu_instance import (
    GpuInstanceOrchestrator,
    NovitaGpuInstanceClient,
    map_instance_status,
)

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import EndpointState


class FakeNovitaInstanceApi:
    def __init__(self) -> None:
        self.instances: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._counter = 0
        self.status = "running"
        self.ip = "10.0.0.42"

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))

        auth = request.headers.get("authorization", "")
        if "bad-key" in auth:
            return httpx.Response(401, json={"message": "invalid credentials"})
        if "forbidden-key" in auth:
            return httpx.Response(403, json={"message": "forbidden"})

        path = request.url.path
        if path == "/gpu-instance/openapi/v1/gpu/instance/create" and request.method == "POST":
            self._counter += 1
            inst_id = f"inst-{self._counter}"
            self.instances[inst_id] = {
                "instance_id": inst_id,
                "status": self.status,
                "ip": self.ip,
                **(body or {}),
            }
            return httpx.Response(200, json={"instance_id": inst_id})

        if path.startswith("/gpu-instance/openapi/v1/gpu/instance/get"):
            inst_id = request.url.params.get("instance_id") or path.rsplit("/", 1)[-1]
            if inst_id not in self.instances:
                return httpx.Response(404, json={"message": f"instance {inst_id} not found"})
            inst = self.instances[inst_id]
            return httpx.Response(200, json={"data": inst})

        if path == "/gpu-instance/openapi/v1/gpu/instance/stop" and request.method == "POST":
            inst_id = (body or {}).get("instance_id")
            if inst_id in self.instances:
                self.instances[inst_id]["status"] = "stopped"
            return httpx.Response(200, json={})

        if path == "/gpu-instance/openapi/v1/gpu/instance/delete" and request.method == "POST":
            inst_id = (body or {}).get("instance_id")
            if inst_id in self.instances:
                self.instances[inst_id]["status"] = "deleted"
            return httpx.Response(200, json={})

        return httpx.Response(404, json={"message": "not found"})


def test_map_instance_status() -> None:
    assert map_instance_status({"status": "running", "ip": "10.0.0.1"}) == (
        EndpointState.READY,
        "http://10.0.0.1:8000/v1",
    )
    assert map_instance_status(
        {"data": {"status": "starting", "url": "https://host.novita.ai/v1"}}
    ) == (
        EndpointState.DEPLOYING,
        "https://host.novita.ai/v1",
    )
    assert map_instance_status({"status": "stopped"}) == (
        EndpointState.SLEEPING,
        None,
    )
    assert map_instance_status({"status": "deleted"}) == (
        EndpointState.RETIRED,
        None,
    )
    assert map_instance_status({"status": "failed"}) == (
        EndpointState.FAILED,
        None,
    )


@pytest.mark.asyncio
async def test_gpu_instance_orchestrator_lifecycle() -> None:
    fake = FakeNovitaInstanceApi()
    transport = httpx.MockTransport(fake.handle)
    client = NovitaGpuInstanceClient(lambda: "valid-key", transport=transport)
    orch = GpuInstanceOrchestrator(client)

    inst_id, _url = await orch.deploy(
        endpoint_id="ep_test",
        image="vllm:digest",
        args=["--arg"],
        gpu_type="A100",
        gpu_count=1,
        hf_token="token",
    )
    assert inst_id == "inst-1"

    state, base_url = await orch.status(inst_id)
    assert state == EndpointState.READY
    assert base_url == "http://10.0.0.42:8000/v1"

    # Scale to 0 (sleep)
    await orch.scale(inst_id, 0, 0)
    state, _ = await orch.status(inst_id)
    assert state == EndpointState.SLEEPING

    # Scale > 1 raises CapabilityNotSupported
    with pytest.raises(CapabilityNotSupported):
        await orch.scale(inst_id, 1, 2)

    # Teardown terminate
    term_state = await orch.teardown(inst_id, "terminate")
    assert term_state == EndpointState.RETIRED
    state, _ = await orch.status(inst_id)
    assert state == EndpointState.RETIRED


@pytest.mark.asyncio
async def test_gpu_instance_client_errors() -> None:
    fake = FakeNovitaInstanceApi()
    transport = httpx.MockTransport(fake.handle)

    bad_key_client = NovitaGpuInstanceClient(lambda: "bad-key", transport=transport)
    with pytest.raises(CredentialRejected):
        await bad_key_client.create_instance({"name": "test"})

    forbidden_client = NovitaGpuInstanceClient(lambda: "forbidden-key", transport=transport)
    with pytest.raises(AccessForbidden):
        await forbidden_client.create_instance({"name": "test"})

    valid_client = NovitaGpuInstanceClient(lambda: "valid-key", transport=transport)
    with pytest.raises(DeploymentNotFound):
        await valid_client.get_instance("nonexistent")
