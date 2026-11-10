"""Internal admin plane: routes, prices, trial keys, usage and events.

One bearer token guards every ``/admin`` route (TECH-DESIGN 13: the admin API is only
ever exposed on the internal network). Inference traffic never reaches this app.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from evalspleef.spi.gateway import PriceSpec
from evalspleef.spi.model import ProviderLock, UpstreamRoute, WireProtocol

from .state import GATEWAY_VERSION, GatewayState

_WIRE_PROTOCOLS: set[str] = {"chat_completions", "responses", "anthropic_messages"}


class ProviderLockIn(BaseModel):
    response_field: str
    pattern: str


class RouteIn(BaseModel):
    """JSON body of ``PUT /admin/routes/{name}``; mirrors :class:`UpstreamRoute`."""

    api_base: str
    model: str
    protocol: WireProtocol
    api_key_ref: str
    default_params: dict[str, Any] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    api_key_header: str | None = None
    provider_lock: ProviderLockIn | None = None
    cold_start_window_s: int = 0
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)

    def to_route(self) -> UpstreamRoute:
        return UpstreamRoute(
            api_base=self.api_base,
            model=self.model,
            protocol=self.protocol,
            api_key_ref=self.api_key_ref,
            default_params=self.default_params,
            extra_headers=self.extra_headers,
            api_key_header=self.api_key_header,
            provider_lock=(
                ProviderLock(
                    response_field=self.provider_lock.response_field,
                    pattern=self.provider_lock.pattern,
                )
                if self.provider_lock is not None
                else None
            ),
            cold_start_window_s=self.cold_start_window_s,
            rpm=self.rpm,
            tpm=self.tpm,
        )


class PriceIn(BaseModel):
    """JSON body of ``PUT /admin/routes/{name}/price``; mirrors :class:`PriceSpec`."""

    input_per_mtok: float
    output_per_mtok: float
    cached_per_mtok: float = 0.0
    basis: Literal["api", "ref-price", "assumed"] = "api"
    table_version: str = ""
    cache_write_per_mtok: float | None = None

    def to_price(self) -> PriceSpec:
        return PriceSpec(
            input_per_mtok=self.input_per_mtok,
            output_per_mtok=self.output_per_mtok,
            cached_per_mtok=self.cached_per_mtok,
            basis=self.basis,
            table_version=self.table_version,
            cache_write_per_mtok=self.cache_write_per_mtok,
        )


class IssueKeyIn(BaseModel):
    """JSON body of ``POST /admin/keys``; mirrors :meth:`GatewayAdmin.issue_key`."""

    trial_id: str
    endpoint_id: str
    max_budget_usd: float
    max_tokens: int
    expires_at: str


def _route_to_json(route: UpstreamRoute) -> dict[str, Any]:
    result = asdict(route)
    result["protocol"] = route.protocol
    return result


def create_admin_app(state: GatewayState, admin_token: str) -> FastAPI:
    """Build the admin application; every route requires the bearer ``admin_token``."""
    app = FastAPI(title="evalspleef-gateway-admin", docs_url=None, redoc_url=None, openapi_url=None)

    def _authorize(request: Request) -> None:
        authorization = request.headers.get("authorization")
        if authorization != f"Bearer {admin_token}":
            raise HTTPException(status_code=401, detail="invalid admin token")

    @app.get("/admin/version")
    async def version(request: Request) -> dict[str, str]:
        _authorize(request)
        return {"version": GATEWAY_VERSION}

    @app.put("/admin/routes/{name}")
    async def put_route(name: str, body: RouteIn, request: Request) -> Response:
        _authorize(request)
        if str(body.protocol) not in _WIRE_PROTOCOLS:
            raise HTTPException(status_code=422, detail=f"unknown protocol: {body.protocol}")
        state.upsert_route(name, body.to_route())
        return JSONResponse({"name": name, "route": _route_to_json(body.to_route())})

    @app.delete("/admin/routes/{name}")
    async def delete_route(name: str, request: Request) -> Response:
        _authorize(request)
        # Idempotent like key revocation: an endpoint that was never probed has no route, and
        # retiring it must not fail after the provider has already stopped the paid resource.
        state.remove_route(name)
        return Response(status_code=204)

    @app.put("/admin/routes/{name}/price")
    async def put_price(name: str, body: PriceIn, request: Request) -> Response:
        _authorize(request)
        try:
            state.set_price(name, body.to_price())
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown route: {name!r}") from None
        return JSONResponse({"name": name, "price": asdict(body.to_price())})

    @app.post("/admin/keys")
    async def issue_key(body: IssueKeyIn, request: Request) -> Response:
        _authorize(request)
        key = await state.issue_key(
            trial_id=body.trial_id,
            endpoint_id=body.endpoint_id,
            max_budget_usd=body.max_budget_usd,
            max_tokens=body.max_tokens,
            expires_at=body.expires_at,
        )
        return JSONResponse(asdict(key), status_code=201)

    @app.delete("/admin/keys/{key_id}")
    async def revoke_key(key_id: str, request: Request) -> Response:
        _authorize(request)
        await state.revoke_key(key_id)
        return Response(status_code=204)

    @app.get("/admin/usage")
    async def usage(trial_id: str, request: Request) -> Response:
        _authorize(request)
        records = await state.usage_for(trial_id)
        return JSONResponse([asdict(record) for record in records])

    @app.get("/admin/events")
    async def events(request: Request, after: int = 0) -> Response:
        _authorize(request)
        return JSONResponse({"events": state.events(after=after)})

    return app
