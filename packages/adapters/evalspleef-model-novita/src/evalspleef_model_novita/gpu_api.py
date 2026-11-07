"""REST client for the Novita Serverless GPUs v2 API.

Paths and field names follow the OpenAPI pages under https://docs.novita.ai/api-reference/
(``gpus-v2-create-endpoint``, ``gpus-v2-get-endpoint``, ``gpus-v2-update-endpoint``,
``gpus-v2-delete-endpoint``). The API key is resolved at the moment of each request, and no
request body is ever logged or echoed into an error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from evalspleef.spi.errors import EndpointUnavailable, SpleefError

from .errors import AccessForbidden, CredentialRejected, DeploymentNotFound, RequestRejected

DEFAULT_API_BASE = "https://api.novita.ai"
ENDPOINTS_PATH = "/gpus/v2/endpoints"
_MESSAGE_CHARS = 200


class NovitaGpuClient:
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
        """Secret values scrubbed out of error messages, in case the vendor echoes them."""

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"authorization": f"Bearer {self._api_key()}"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(self._timeout_s)
            ) as client:
                response = await client.request(
                    method, self._api_base + path, json=body, headers=headers
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
                f"{method} {path}: HTTP {response.status_code} with a body that is not JSON",
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

    async def create_endpoint(self, body: dict[str, Any]) -> str:
        payload = await self._request("POST", ENDPOINTS_PATH, body=body)
        endpoint_id = payload.get("id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise EndpointUnavailable(
                "POST /gpus/v2/endpoints answered without an endpoint id",
                detail={"reason": "malformed"},
            )
        return endpoint_id

    async def get_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{ENDPOINTS_PATH}/{endpoint_id}")

    async def update_endpoint(self, endpoint_id: str, body: dict[str, Any]) -> None:
        await self._request("PUT", f"{ENDPOINTS_PATH}/{endpoint_id}", body=body)

    async def delete_endpoint(self, endpoint_id: str) -> None:
        await self._request("DELETE", f"{ENDPOINTS_PATH}/{endpoint_id}")
