"""Novita GPU Instance REST API client and orchestrator for vpc-vllm endpoints.

Interacts with Novita's GPU Instance OpenAPI endpoints to provision, inspect,
and tear down dedicated GPU instances running containerized vLLM.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from evalspleef.spi.errors import CapabilityNotSupported, EndpointUnavailable, SpleefError
from evalspleef.spi.model import EndpointState, TeardownMode

from .errors import AccessForbidden, CredentialRejected, DeploymentNotFound, RequestRejected

DEFAULT_API_BASE = "https://api.novita.ai"
INSTANCE_CREATE_PATH = "/gpu-instance/openapi/v1/gpu/instance/create"
INSTANCE_GET_PATH = "/gpu-instance/openapi/v1/gpu/instance/get"
INSTANCE_STOP_PATH = "/gpu-instance/openapi/v1/gpu/instance/stop"
INSTANCE_DELETE_PATH = "/gpu-instance/openapi/v1/gpu/instance/delete"
_MESSAGE_CHARS = 200

_RUNNING = frozenset({"running", "ready", "active", "online", "healthy"})
_STARTING = frozenset({"creating", "starting", "provisioning", "pending", "initializing"})
_STOPPED = frozenset({"stopped", "stopping", "shutdown", "shut_down", "idle", "paused"})
_TERMINATED = frozenset({"deleted", "terminating", "terminated", "destroyed"})
_FAILED = frozenset({"failed", "error", "unhealthy", "abnormal"})


class NovitaGpuInstanceClient:
    """REST client for the Novita GPU Instance API."""

    def __init__(
        self,
        api_key: Callable[[], str],
        *,
        api_base: str = DEFAULT_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
        hidden: Callable[[], tuple[str, ...]] = lambda: (),
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._transport = transport
        self._timeout_s = timeout_s
        self._hidden = hidden

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"authorization": f"Bearer {self._api_key()}"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(self._timeout_s)
            ) as client:
                response = await client.request(
                    method,
                    self._api_base + path,
                    json=body,
                    params=params,
                    headers=headers,
                )
        except httpx.TransportError as exc:
            raise EndpointUnavailable(
                f"{method} {path}: {type(exc).__name__}", detail={"reason": "transport"}
            ) from exc

        if response.status_code >= 400:
            raise self._error(method, path, response)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise EndpointUnavailable(
                f"{method} {path}: HTTP {response.status_code} with non-JSON body",
                detail={"reason": "malformed"},
            ) from exc
        return payload if isinstance(payload, dict) else {}

    def _error(self, method: str, path: str, response: httpx.Response) -> SpleefError:
        status = response.status_code
        message = ""
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("reason") or "")
        for secret in self._hidden():
            if secret:
                message = message.replace(secret, "***")
        context = f"{method} {path}: HTTP {status}"
        if message:
            context += f" {message[:_MESSAGE_CHARS]}"
        detail = {"reason": f"http_{status}", "http_status": status}
        if status == 401:
            return CredentialRejected(context, detail=detail)
        if status == 403:
            return AccessForbidden(context, detail=detail)
        if status == 404:
            return DeploymentNotFound(context, detail=detail)
        if status == 429 or status >= 500:
            return EndpointUnavailable(context, detail=detail)
        return RequestRejected(context, detail=detail)

    async def create_instance(self, body: dict[str, Any]) -> str:
        payload = await self._request("POST", INSTANCE_CREATE_PATH, body=body)
        instance_id = (
            payload.get("instance_id")
            or payload.get("id")
            or (payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}).get(
                "instance_id"
            )
        )
        if not isinstance(instance_id, str) or not instance_id:
            raise EndpointUnavailable(
                f"POST {INSTANCE_CREATE_PATH} answered without instance id",
                detail={"reason": "malformed"},
            )
        return instance_id

    async def get_instance(self, instance_id: str) -> dict[str, Any]:
        # Supports query param or POST/GET body/path convention
        try:
            return await self._request("GET", f"{INSTANCE_GET_PATH}/{instance_id}")
        except DeploymentNotFound:
            return await self._request(
                "GET", INSTANCE_GET_PATH, params={"instance_id": instance_id}
            )

    async def stop_instance(self, instance_id: str) -> None:
        await self._request("POST", INSTANCE_STOP_PATH, body={"instance_id": instance_id})

    async def delete_instance(self, instance_id: str) -> None:
        await self._request("POST", INSTANCE_DELETE_PATH, body={"instance_id": instance_id})


def map_instance_status(payload: Mapping[str, Any]) -> tuple[EndpointState, str | None]:
    """Map instance API response to (EndpointState, base_url | None)."""
    raw_data = payload.get("data")
    data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else payload
    raw_status = str(data.get("status") or "").lower()

    base_url: str | None = None
    url_val = data.get("url")
    ip_val = data.get("ip")
    public_ip_val = data.get("public_ip")
    if isinstance(url_val, str):
        base_url = url_val
    elif isinstance(ip_val, str):
        base_url = f"http://{ip_val}:8000/v1"
    elif isinstance(public_ip_val, str):
        base_url = f"http://{public_ip_val}:8000/v1"

    if raw_status in _RUNNING:
        return EndpointState.READY, base_url
    if raw_status in _STARTING:
        return EndpointState.DEPLOYING, base_url
    if raw_status in _STOPPED:
        return EndpointState.SLEEPING, base_url
    if raw_status in _TERMINATED:
        return EndpointState.RETIRED, None
    if raw_status in _FAILED:
        return EndpointState.FAILED, None
    return EndpointState.DEGRADED, base_url


class GpuInstanceOrchestrator:
    """GPU instance orchestrator for vLLM endpoints."""

    def __init__(
        self,
        client: NovitaGpuInstanceClient,
        *,
        url_template: str | None = None,
    ) -> None:
        self.client = client
        self.url_template = url_template

    async def deploy(
        self,
        *,
        endpoint_id: str,
        image: str,
        args: list[str],
        gpu_type: str | None,
        gpu_count: int | None,
        hf_token: str | None,
        extra_env: dict[str, str] | None = None,
        port: int = 8000,
    ) -> tuple[str, str | None]:
        """Deploy an instance. Returns (instance_id, initial_base_url)."""
        envs: dict[str, str] = {}
        if hf_token:
            envs["HUGGING_FACE_HUB_TOKEN"] = hf_token
        if extra_env:
            envs.update(extra_env)

        body: dict[str, Any] = {
            "name": endpoint_id,
            "image": image,
            "command": args,
            "envs": envs,
            "ports": [port],
        }
        if gpu_type:
            body["gpu_type"] = gpu_type
        if gpu_count:
            body["gpu_num"] = gpu_count

        instance_id = await self.client.create_instance(body)
        initial_url = (
            self.url_template.format(instance_id=instance_id) if self.url_template else None
        )
        return instance_id, initial_url

    async def status(self, instance_id: str) -> tuple[EndpointState, str | None]:
        payload = await self.client.get_instance(instance_id)
        state, base_url = map_instance_status(payload)
        if self.url_template and base_url is None:
            base_url = self.url_template.format(instance_id=instance_id)
        return state, base_url

    async def scale(self, instance_id: str, min_replicas: int, max_replicas: int) -> None:
        if min_replicas == 0:
            await self.client.stop_instance(instance_id)
            return
        if max_replicas > 1:
            raise CapabilityNotSupported(
                "scale > 1 is not supported for standalone GPU instance",
                detail={"max_replicas": max_replicas},
            )

    async def teardown(self, instance_id: str, mode: TeardownMode) -> EndpointState:
        if mode == "sleep":
            await self.client.stop_instance(instance_id)
            return EndpointState.SLEEPING
        await self.client.delete_instance(instance_id)
        return EndpointState.RETIRED
