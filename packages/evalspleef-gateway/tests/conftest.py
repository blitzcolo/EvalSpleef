"""Shared fixtures for the gateway tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.data_app import create_data_app
from evalspleef.gateway.state import GatewayState
from evalspleef.gateway.store import MemoryStore
from evalspleef.testing.secrets import DictSecretResolver

EXPIRES_AT = "2099-01-01T00:00:00Z"
UPSTREAM_KEY_REF = "vault://upstream/key"
UPSTREAM_KEY = "upstream-secret"

TEST_ROUTE = {
    "api_base": "http://upstream.test/v1",
    "model": "fake-model",
    "protocol": "chat_completions",
    "api_key_ref": UPSTREAM_KEY_REF,
}


def make_state(**kwargs: object) -> GatewayState:
    store = MemoryStore()
    defaults: dict[str, object] = {
        "key_store": store,
        "usage_store": store,
        "secrets": DictSecretResolver({UPSTREAM_KEY_REF: UPSTREAM_KEY}),
    }
    defaults.update(kwargs)
    return GatewayState(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def state() -> GatewayState:
    return make_state()


@pytest.fixture
async def data_client(state: GatewayState) -> AsyncIterator[httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_data_app(state)), base_url="http://gw.test"
    )
    async with client as client:
        yield client


@pytest.fixture
async def admin_client(state: GatewayState) -> AsyncIterator[httpx.AsyncClient]:
    app = create_admin_app(state, "admin-token")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw-admin.test"
    )
    async with client as client:
        yield client


def auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}
