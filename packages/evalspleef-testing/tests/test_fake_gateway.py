from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP, PriceSpec, TrialKey
from evalspleef.spi.model import UpstreamRoute
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin, TrialKeyRejected

ROUTE = "ep_qwen_test"
START = 1_700_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


async def make_admin() -> tuple[InMemoryGatewayAdmin, FakeClock]:
    clock = FakeClock(start=START)
    admin = InMemoryGatewayAdmin(clock=clock)
    await admin.upsert_route(
        ROUTE,
        UpstreamRoute(
            api_base="http://upstream.test/v1",
            model="qwen3-8b",
            protocol="chat_completions",
            api_key_ref="env://UPSTREAM_KEY",
        ),
    )
    await admin.set_price(
        ROUTE, PriceSpec(input_per_mtok=1.0, output_per_mtok=2.0, cached_per_mtok=0.5)
    )
    return admin, clock


async def issue(
    admin: InMemoryGatewayAdmin,
    clock: FakeClock,
    *,
    trial_id: str = "t1",
    max_budget_usd: float = 1.0,
    max_tokens: int = 1_000_000,
    ttl_s: float = 600.0,
) -> TrialKey:
    return await admin.issue_key(
        trial_id=trial_id,
        endpoint_id=ROUTE,
        max_budget_usd=max_budget_usd,
        max_tokens=max_tokens,
        expires_at=iso(clock.now() + ttl_s),
    )


async def test_route_and_version_management() -> None:
    admin, _ = await make_admin()
    assert await admin.version() == "in-memory-gateway/0.1"
    assert admin.routes[ROUTE].model == "qwen3-8b"

    await admin.remove_route(ROUTE)
    await admin.remove_route(ROUTE)
    assert ROUTE not in admin.routes
    with pytest.raises(KeyError, match=ROUTE):
        admin.record_usage(
            trial_id="t1", round=0, route_name=ROUTE, input_tokens=1, output_tokens=1
        )


async def test_issued_key_binds_trial_and_endpoint() -> None:
    admin, clock = await make_admin()
    first = await issue(admin, clock)
    second = await issue(admin, clock, trial_id="t2")

    assert first.trial_id == "t1"
    assert first.endpoint_id == ROUTE
    assert first.max_budget_usd == 1.0
    assert first.max_tokens == 1_000_000
    assert first.expires_at == iso(START + 600.0)
    assert first.secret != second.secret
    assert admin.authenticate(first.secret) == first


async def test_expired_key_is_rejected() -> None:
    admin, clock = await make_admin()
    key = await issue(admin, clock, ttl_s=60.0)

    assert admin.authenticate(key.secret) == key
    clock.advance(59.0)
    assert admin.authenticate(key.secret) == key
    clock.advance(1.0)
    with pytest.raises(TrialKeyRejected) as caught:
        admin.authenticate(key.secret)
    assert caught.value.reason == "expired"


async def test_revoked_key_is_rejected_and_revocation_is_idempotent() -> None:
    admin, clock = await make_admin()
    key = await issue(admin, clock)
    other = await issue(admin, clock, trial_id="t2")

    await admin.revoke_key(key.key_id)
    await admin.revoke_key(key.key_id)

    assert admin.is_revoked(key.key_id)
    with pytest.raises(TrialKeyRejected) as caught:
        admin.authenticate(key.secret)
    assert caught.value.reason == "revoked"
    assert admin.authenticate(other.secret) == other


async def test_unknown_key_is_rejected() -> None:
    admin, _ = await make_admin()
    with pytest.raises(TrialKeyRejected) as caught:
        admin.authenticate("sk-spleef-not-issued")
    assert caught.value.reason == "unknown"


async def test_record_usage_costs_from_the_frozen_price_and_stamps_the_clock() -> None:
    admin, clock = await make_admin()
    await issue(admin, clock)

    record = admin.record_usage(
        trial_id="t1",
        round=2,
        route_name=ROUTE,
        input_tokens=3000,
        output_tokens=1000,
        cached_tokens=200,
        reasoning_tokens=50,
        latency_ms=120,
    )

    assert record.ts == iso(START)
    assert record.endpoint_id == ROUTE
    assert record.upstream_model == "qwen3-8b"
    assert record.cost_usd == pytest.approx((3000 * 1.0 + 1000 * 2.0 + 200 * 0.5) / 1_000_000)
    assert await admin.usage("t1") == [record]
    assert admin.spent_usd("t1") == pytest.approx(0.0051)
    assert admin.tokens_used("t1") == 4000
    assert admin.budget_status("t1") is None


async def test_budget_status_reports_cost_cap() -> None:
    admin, clock = await make_admin()
    await issue(admin, clock, max_budget_usd=0.004)

    admin.record_usage(
        trial_id="t1", round=0, route_name=ROUTE, input_tokens=3000, output_tokens=1000
    )

    assert admin.spent_usd("t1") == pytest.approx(0.005)
    assert admin.budget_status("t1") == COST_CAP


async def test_budget_status_reports_token_cap() -> None:
    admin, clock = await make_admin()
    await issue(admin, clock, max_budget_usd=10.0, max_tokens=4000)

    admin.record_usage(
        trial_id="t1", round=0, route_name=ROUTE, input_tokens=3000, output_tokens=1000
    )

    assert admin.tokens_used("t1") == 4000
    assert admin.budget_status("t1") == TOKEN_CAP


async def test_usage_is_kept_per_trial() -> None:
    admin, clock = await make_admin()
    await issue(admin, clock, trial_id="t1")
    await issue(admin, clock, trial_id="t2")

    first = admin.record_usage(
        trial_id="t1", round=0, route_name=ROUTE, input_tokens=10, output_tokens=5
    )
    second = admin.record_usage(
        trial_id="t2", round=0, route_name=ROUTE, input_tokens=20, output_tokens=7
    )

    assert await admin.usage("t1") == [first]
    assert await admin.usage("t2") == [second]
    assert admin.spent_usd("t1") == pytest.approx(first.cost_usd)
    assert admin.tokens_used("t2") == 27


async def test_record_usage_requires_a_priced_route() -> None:
    admin = InMemoryGatewayAdmin(clock=FakeClock(start=START))
    await admin.upsert_route(
        ROUTE,
        UpstreamRoute(
            api_base="http://upstream.test/v1",
            model="qwen3-8b",
            protocol="chat_completions",
            api_key_ref="env://UPSTREAM_KEY",
        ),
    )

    with pytest.raises(KeyError, match="no price"):
        admin.record_usage(
            trial_id="t1", round=0, route_name=ROUTE, input_tokens=1, output_tokens=1
        )
    with pytest.raises(KeyError, match="no route"):
        admin.record_usage(
            trial_id="t1", round=0, route_name="ep_missing", input_tokens=1, output_tokens=1
        )
