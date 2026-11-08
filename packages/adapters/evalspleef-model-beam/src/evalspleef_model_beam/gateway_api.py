"""REST client for the Beam gateway HTTP API (``https://app.beam.cloud/api/v1/gateway``).

Paths and field names follow ``gateway.swagger.json`` published under
https://docs.beam.cloud/v2/reference/api-docs/. The gateway can answer HTTP 200 with
``ok: false``, so both layers are checked. The token is resolved at the moment of each request,
and no request body is ever logged or echoed into an error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from evalspleef.spi.errors import EndpointUnavailable, SpleefError

from .errors import AccessForbidden, CredentialRejected, DeploymentNotFound, RequestRejected

DEFAULT_API_BASE = "https://app.beam.cloud/api/v1/gateway"
DEFAULT_REST_BASE = "https://app.beam.cloud/api/v1"
"""The workspace REST API (tasks, workspace); unlike the gateway it reports errors by HTTP status."""
_MESSAGE_CHARS = 200


class BeamGatewayClient:
    def __init__(
        self,
        token: Callable[[], str],
        *,
        api_base: str = DEFAULT_API_BASE,
        rest_base: str = DEFAULT_REST_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
        hidden: Callable[[], tuple[str, ...]] = lambda: (),
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._rest_base = rest_base.rstrip("/")
        self._transport = transport
        self._timeout_s = timeout_s
        self._hidden = hidden
        """Secret values scrubbed out of error messages, in case the vendor echoes them."""

    def _scrub(self, text: str) -> str:
        for secret in self._hidden():
            if secret:
                text = text.replace(secret, "***")
        return text[:_MESSAGE_CHARS]

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, rest: bool = False
    ) -> dict[str, Any]:
        headers = {"authorization": f"Bearer {self._token()}"}
        base = self._rest_base if rest else self._api_base
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(self._timeout_s)
            ) as client:
                response = await client.request(method, base + path, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise EndpointUnavailable(
                f"{method} {path}: {type(exc).__name__}", detail={"reason": "transport"}
            ) from exc
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                if response.status_code < 400:
                    raise EndpointUnavailable(
                        f"{method} {path}: HTTP {response.status_code} with a body that is not JSON",
                        detail={"reason": "malformed"},
                    ) from exc
        record: dict[str, Any] = payload if isinstance(payload, dict) else {}
        message = self._scrub(
            str(record.get("errMsg") or record.get("errorMsg") or record.get("message") or "")
        )
        if response.status_code >= 400:
            raise self._http_error(method, path, response.status_code, message)
        if record.get("ok") is False:
            context = f"{method} {path}: ok=false {message}".rstrip()
            detail = {"reason": "gateway_not_ok"}
            if "not found" in message.lower():
                raise DeploymentNotFound(context, detail=detail)
            raise RequestRejected(context, detail=detail)
        return record

    @staticmethod
    def _http_error(method: str, path: str, status: int, message: str) -> SpleefError:
        context = f"{method} {path}: HTTP {status}"
        if message:
            context += f" {message}"
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

    async def list_deployments(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/deployments")
        items = payload.get("deployments")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def list_containers(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/containers")
        items = payload.get("containers")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def scale_deployment(self, deployment_id: str, containers: int) -> None:
        await self._request(
            "POST", f"/deployments/{deployment_id}/scale", body={"containers": containers}
        )

    async def stop_deployment(self, deployment_id: str) -> None:
        await self._request("POST", f"/deployments/{deployment_id}/stop")

    async def delete_deployment(self, deployment_id: str) -> None:
        await self._request("DELETE", f"/deployments/{deployment_id}")

    async def stop_container(self, container_id: str) -> None:
        await self._request("POST", f"/containers/{container_id}/stop")

    async def current_workspace_id(self) -> str:
        payload = await self._request("GET", "/workspace/current", rest=True)
        workspace_id = payload.get("external_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise EndpointUnavailable(
                "GET /workspace/current answered without an external_id",
                detail={"reason": "malformed"},
            )
        return workspace_id

    async def get_task(self, workspace_id: str, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/task/{workspace_id}/{task_id}", rest=True)
