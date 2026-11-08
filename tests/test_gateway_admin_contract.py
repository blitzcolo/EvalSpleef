"""The control plane's admin client and the gateway's admin app agree on the route contract."""

from __future__ import annotations

import httpx

from evalspleef.control.cli.gateway_admin import HttpGatewayAdmin
from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.state import GatewayState
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.model import ProviderLock, UpstreamRoute
from evalspleef.testing.secrets import DictSecretResolver


async def test_a_route_set_by_the_cli_reaches_the_gateway_with_every_field() -> None:
    store = MemoryStore()
    state = GatewayState(key_store=store, usage_store=store, secrets=DictSecretResolver({}))
    transport = httpx.ASGITransport(app=create_admin_app(state, "token"))
    route = UpstreamRoute(
        api_base="http://upstream.test/v1",
        model="m",
        protocol="chat_completions",
        api_key_ref="env://KEY",
        default_params={"temperature": 0},
        extra_headers={"x-a": "1"},
        provider_lock=ProviderLock(response_field="provider", pattern="^acme$"),
        cold_start_window_s=30,
        rpm=12,
        tpm=34_000,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        admin = HttpGatewayAdmin("http://gw-admin.test", "token", client=client)
        await admin.upsert_route("ep_m", route)
    entry = state.route_entry("ep_m")
    assert entry is not None and entry.route == route
    await state.close()
