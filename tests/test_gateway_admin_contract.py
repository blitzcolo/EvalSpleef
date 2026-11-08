"""The control plane's admin client and the gateway's admin app agree on the route contract."""

from __future__ import annotations

import httpx

from evalspleef.control.cli.gateway_admin import HttpGatewayAdmin
from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.data_app import create_data_app
from evalspleef.gateway.state import GatewayState
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.gateway import PriceSpec
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


async def test_a_key_issued_by_the_cli_calls_its_endpoint_by_the_endpoint_id() -> None:
    """The SPI names the gateway model after the endpoint id itself (``ep_<slug>_<hash>``)."""
    endpoint_id = "ep_qwen_1a2b3c4d"
    store = MemoryStore()
    state = GatewayState(
        key_store=store, usage_store=store, secrets=DictSecretResolver({"env://KEY": "k"})
    )
    state.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "chat.completion",
                    "model": "upstream-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        )
    )
    route = UpstreamRoute(
        api_base="http://upstream.test/v1",
        model="upstream-model",
        protocol="chat_completions",
        api_key_ref="env://KEY",
    )
    admin_transport = httpx.ASGITransport(app=create_admin_app(state, "token"))
    async with httpx.AsyncClient(transport=admin_transport) as client:
        admin = HttpGatewayAdmin("http://gw-admin.test", "token", client=client)
        await admin.upsert_route(endpoint_id, route)
        await admin.set_price(endpoint_id, PriceSpec(input_per_mtok=0.0, output_per_mtok=0.0))
        key = await admin.issue_key(
            trial_id="t1",
            endpoint_id=endpoint_id,
            max_budget_usd=1.0,
            max_tokens=1000,
            expires_at="2099-01-01T00:00:00Z",
        )
    data_transport = httpx.ASGITransport(app=create_data_app(state))
    async with httpx.AsyncClient(transport=data_transport, base_url="http://gw.test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": endpoint_id, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key.secret}"},
        )
    assert response.status_code == 200, response.text
    assert [record.endpoint_id for record in await state.usage_for("t1")] == [endpoint_id]
    await state.close()
