"""Admin plane tests: HTTP admin app, HttpGatewayAdmin and InProcessGatewayAdmin."""

from __future__ import annotations

import httpx
import pytest

from evalspleef.gateway.admin_client import HttpGatewayAdmin, InProcessGatewayAdmin
from evalspleef.gateway.state import GATEWAY_VERSION, GatewayState
from evalspleef.spi.gateway import PriceSpec
from evalspleef.spi.model import UpstreamRoute

from .conftest import EXPIRES_AT, TEST_ROUTE

ADMIN = {"Authorization": "Bearer admin-token"}


def route(**overrides: object) -> UpstreamRoute:
    raw = dict(TEST_ROUTE)
    raw.update(overrides)
    return UpstreamRoute(
        api_base=raw["api_base"],  # type: ignore[arg-type]
        model=raw["model"],  # type: ignore[arg-type]
        protocol=raw["protocol"],  # type: ignore[arg-type]
        api_key_ref=raw["api_key_ref"],  # type: ignore[arg-type]
    )


async def test_admin_requires_bearer_token(
    state: GatewayState, admin_client: httpx.AsyncClient
) -> None:
    response = await admin_client.get("/admin/version")
    assert response.status_code == 401
    response = await admin_client.get("/admin/version", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    response = await admin_client.get("/admin/version", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"version": GATEWAY_VERSION}


async def test_route_and_price_management(
    state: GatewayState, admin_client: httpx.AsyncClient
) -> None:
    response = await admin_client.put("/admin/routes/ep_ep1", json=TEST_ROUTE, headers=ADMIN)
    assert response.status_code == 200
    assert state.route_entry("ep_ep1") is not None

    price = {
        "input_per_mtok": 1.5,
        "output_per_mtok": 2.5,
        "cached_per_mtok": 0.5,
        "basis": "api",
        "table_version": "v1",
    }
    response = await admin_client.put("/admin/routes/ep_ep1/price", json=price, headers=ADMIN)
    assert response.status_code == 200
    entry = state.route_entry("ep_ep1")
    assert entry is not None
    assert entry.price == PriceSpec(
        input_per_mtok=1.5,
        output_per_mtok=2.5,
        cached_per_mtok=0.5,
        basis="api",
        table_version="v1",
    )

    # Price on an unknown route is a 404.
    response = await admin_client.put("/admin/routes/ep_nope/price", json=price, headers=ADMIN)
    assert response.status_code == 404

    response = await admin_client.delete("/admin/routes/ep_ep1", headers=ADMIN)
    assert response.status_code == 204
    assert state.route_entry("ep_ep1") is None
    response = await admin_client.delete("/admin/routes/ep_ep1", headers=ADMIN)
    assert response.status_code == 404


async def test_issue_and_revoke_key(state: GatewayState, admin_client: httpx.AsyncClient) -> None:
    body = {
        "trial_id": "trial-1",
        "endpoint_id": "ep1",
        "max_budget_usd": 5.0,
        "max_tokens": 1000,
        "expires_at": EXPIRES_AT,
    }
    response = await admin_client.post("/admin/keys", json=body, headers=ADMIN)
    assert response.status_code == 201
    issued = response.json()
    assert issued["secret"].startswith("sk-trial-")
    assert issued["key_id"] in issued["secret"] or issued["key_id"]

    # The plaintext secret authenticates; the store holds only its hash.
    stored = await state.authenticate(issued["secret"])
    assert stored is not None and stored.key_id == issued["key_id"]
    raw = await state.key_store.key_by_id(issued["key_id"])
    assert raw is not None and raw.secret_sha256 != issued["secret"]

    response = await admin_client.delete(f"/admin/keys/{issued['key_id']}", headers=ADMIN)
    assert response.status_code == 204
    response = await admin_client.delete(f"/admin/keys/{issued['key_id']}", headers=ADMIN)
    assert response.status_code == 204  # idempotent
    assert await state.authenticate(issued["secret"]) is None


async def test_usage_and_events_endpoints(
    state: GatewayState, admin_client: httpx.AsyncClient
) -> None:
    key = await state.issue_key(
        trial_id="trial-1",
        endpoint_id="ep1",
        max_budget_usd=1.0,
        max_tokens=100,
        expires_at=EXPIRES_AT,
    )
    await state.charge(key.key_id, cost_usd=0.1, tokens=42)
    from evalspleef.spi.gateway import UsageRecord

    await state.append_usage(
        UsageRecord(
            ts="2026-01-01T00:00:00+00:00",
            trial_id="trial-1",
            round=1,
            endpoint_id="ep1",
            upstream_model="fake-model",
            input_tokens=30,
            output_tokens=12,
            cached_tokens=0,
            reasoning_tokens=0,
            latency_ms=7,
            status_code=200,
            cold_start_ms=0,
            cost_usd=0.1,
        )
    )

    response = await admin_client.get("/admin/usage", params={"trial_id": "trial-1"}, headers=ADMIN)
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    assert records[0]["input_tokens"] == 30
    assert records[0]["round"] == 1

    response = await admin_client.get("/admin/events", headers=ADMIN)
    assert response.status_code == 200
    events = response.json()["events"]
    assert events[0]["type"] == "key.issued"
    seq = events[0]["seq"]
    response = await admin_client.get("/admin/events", params={"after": seq}, headers=ADMIN)
    rest = response.json()["events"]
    assert all(event["seq"] > seq for event in rest)
    assert len(rest) == len(events) - 1


# -- GatewayAdmin implementations -------------------------------------------------------


async def test_in_process_gateway_admin(state: GatewayState) -> None:
    admin = InProcessGatewayAdmin(state)
    assert await admin.version() == GATEWAY_VERSION
    await admin.upsert_route("ep_ep1", route())
    await admin.set_price("ep_ep1", PriceSpec(input_per_mtok=1.0, output_per_mtok=2.0))
    key = await admin.issue_key(
        trial_id="trial-1",
        endpoint_id="ep1",
        max_budget_usd=1.0,
        max_tokens=100,
        expires_at=EXPIRES_AT,
    )
    assert await state.authenticate(key.secret) is not None
    await admin.revoke_key(key.key_id)
    await admin.revoke_key(key.key_id)  # idempotent
    assert await state.authenticate(key.secret) is None


async def test_http_gateway_admin_over_asgi(state: GatewayState) -> None:
    from evalspleef.gateway.admin_app import create_admin_app

    transport = httpx.ASGITransport(app=create_admin_app(state, "admin-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as client:
        admin = HttpGatewayAdmin("http://admin.test", "admin-token", client=client)
        assert await admin.version() == GATEWAY_VERSION

        await admin.upsert_route("ep_ep1", route())
        await admin.set_price("ep_ep1", PriceSpec(input_per_mtok=1.0, output_per_mtok=2.0))
        key = await admin.issue_key(
            trial_id="trial-1",
            endpoint_id="ep1",
            max_budget_usd=1.0,
            max_tokens=100,
            expires_at=EXPIRES_AT,
        )
        assert key.secret.startswith("sk-trial-")
        assert await state.authenticate(key.secret) is not None

        await admin.revoke_key(key.key_id)
        assert await state.authenticate(key.secret) is None

        records = await admin.usage("trial-1")
        assert records == []

        await admin.remove_route("ep_ep1")
        assert state.route_entry("ep_ep1") is None


async def test_http_gateway_admin_raises_on_bad_token(state: GatewayState) -> None:
    from evalspleef.gateway.admin_app import create_admin_app

    transport = httpx.ASGITransport(app=create_admin_app(state, "admin-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as client:
        admin = HttpGatewayAdmin("http://admin.test", "wrong-token", client=client)
        with pytest.raises(RuntimeError, match="gateway admin error 401"):
            await admin.version()
