"""HTTP client for the gateway admin plane.

The control plane must not import the gateway package (import-linter), so the
CLI carries its own small admin client speaking exactly the HTTP contract of
``evalspleef.gateway.admin_app``: one bearer token, internal network only.
It implements the SPI :class:`~evalspleef.spi.gateway.GatewayAdmin`.
"""

from __future__ import annotations

from typing import Any

import httpx

from evalspleef.spi.gateway import GatewayAdmin, PriceSpec, TrialKey, UsageRecord
from evalspleef.spi.model import UpstreamRoute

__all__ = ["HttpGatewayAdmin"]


class HttpGatewayAdmin(GatewayAdmin):
    """Admin client for a gateway process reachable over HTTP."""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = admin_token
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        """Release the client when this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise RuntimeError(f"gateway admin error {response.status_code}: {response.text}")

    @staticmethod
    def _route_body(route: UpstreamRoute) -> dict[str, Any]:
        lock = route.provider_lock
        return {
            "api_base": route.api_base,
            "model": route.model,
            "protocol": route.protocol,
            "api_key_ref": route.api_key_ref,
            "default_params": dict(route.default_params),
            "extra_headers": dict(route.extra_headers),
            "api_key_header": route.api_key_header,
            "provider_lock": (
                None
                if lock is None
                else {"response_field": lock.response_field, "pattern": lock.pattern}
            ),
            "cold_start_window_s": route.cold_start_window_s,
            "rpm": route.rpm,
            "tpm": route.tpm,
        }

    async def version(self) -> str:
        response = await self._client.get(
            f"{self._base_url}/admin/version", headers=self._headers()
        )
        await self._raise_for_status(response)
        body = response.json()
        return str(body["version"])

    async def upsert_route(self, name: str, route: UpstreamRoute) -> None:
        response = await self._client.put(
            f"{self._base_url}/admin/routes/{name}",
            json=self._route_body(route),
            headers=self._headers(),
        )
        await self._raise_for_status(response)

    async def remove_route(self, name: str) -> None:
        response = await self._client.delete(
            f"{self._base_url}/admin/routes/{name}", headers=self._headers()
        )
        await self._raise_for_status(response)

    async def set_price(self, name: str, price: PriceSpec) -> None:
        body = {
            "input_per_mtok": price.input_per_mtok,
            "output_per_mtok": price.output_per_mtok,
            "cached_per_mtok": price.cached_per_mtok,
            "basis": price.basis,
            "table_version": price.table_version,
            "cache_write_per_mtok": price.cache_write_per_mtok,
        }
        response = await self._client.put(
            f"{self._base_url}/admin/routes/{name}/price",
            json=body,
            headers=self._headers(),
        )
        await self._raise_for_status(response)

    async def issue_key(
        self,
        *,
        trial_id: str,
        endpoint_id: str,
        max_budget_usd: float,
        max_tokens: int,
        expires_at: str,
    ) -> TrialKey:
        body = {
            "trial_id": trial_id,
            "endpoint_id": endpoint_id,
            "max_budget_usd": max_budget_usd,
            "max_tokens": max_tokens,
            "expires_at": expires_at,
        }
        response = await self._client.post(
            f"{self._base_url}/admin/keys", json=body, headers=self._headers()
        )
        await self._raise_for_status(response)
        data = response.json()
        return TrialKey(
            key_id=str(data["key_id"]),
            secret=str(data["secret"]),
            trial_id=str(data["trial_id"]),
            endpoint_id=str(data["endpoint_id"]),
            max_budget_usd=float(data["max_budget_usd"]),
            max_tokens=int(data["max_tokens"]),
            expires_at=str(data["expires_at"]),
        )

    async def revoke_key(self, key_id: str) -> None:
        response = await self._client.delete(
            f"{self._base_url}/admin/keys/{key_id}", headers=self._headers()
        )
        await self._raise_for_status(response)

    async def usage(self, trial_id: str) -> list[UsageRecord]:
        response = await self._client.get(
            f"{self._base_url}/admin/usage",
            params={"trial_id": trial_id},
            headers=self._headers(),
        )
        await self._raise_for_status(response)
        return [UsageRecord(**record) for record in response.json()]
