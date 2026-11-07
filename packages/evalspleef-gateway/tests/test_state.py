"""Key lifecycle, budget accounting and events on :class:`GatewayState`."""

from __future__ import annotations

from evalspleef.gateway.state import (
    EVENT_KEY_ISSUED,
    EVENT_KEY_REVOKED,
    BudgetProjection,
    hash_secret,
)
from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP

from .conftest import EXPIRES_AT


async def _issue(state, **overrides: object):
    params: dict[str, object] = {
        "trial_id": "trial-1",
        "endpoint_id": "ep1",
        "max_budget_usd": 1.0,
        "max_tokens": 1000,
        "expires_at": EXPIRES_AT,
    }
    params.update(overrides)
    return await state.issue_key(**params)  # type: ignore[arg-type]


async def test_issue_key_returns_secret_once_and_stores_only_hash(state) -> None:
    key = await _issue(state)
    assert key.secret.startswith("sk-trial-")
    stored = await state.key_store.key_by_id(key.key_id)
    assert stored is not None
    assert stored.secret_sha256 == hash_secret(key.secret)
    assert key.secret not in repr(stored)
    by_hash = await state.key_store.key_by_secret_hash(hash_secret(key.secret))
    assert by_hash is not None and by_hash.key_id == key.key_id
    events = state.events()
    assert events[0]["type"] == EVENT_KEY_ISSUED
    assert events[0]["trial_id"] == "trial-1"


async def test_unknown_revoked_and_expired_keys_do_not_authenticate(state) -> None:
    key = await _issue(state)
    assert await state.authenticate(key.secret) is not None
    assert await state.authenticate("sk-trial-nope") is None

    assert await state.revoke_key(key.key_id) is True
    assert await state.authenticate(key.secret) is None
    # Idempotent: second revoke reports no transition and emits no extra event.
    count = len(state.events())
    assert await state.revoke_key(key.key_id) is False
    assert len(state.events()) == count

    expired = await _issue(state, expires_at="2001-01-01T00:00:00Z")
    assert await state.authenticate(expired.secret) is None


async def test_budget_check_rejects_cost_cap_then_token_cap(state) -> None:
    key = await _issue(state, max_budget_usd=0.5, max_tokens=100)
    stored = await state.authenticate(key.secret)
    assert stored is not None
    await state.charge(key.key_id, cost_usd=0.4, tokens=60)

    # Spent 0.4 + projection 0.2 exceeds the 0.5 cost budget.
    violation = await state.check_budget(stored, BudgetProjection(cost_usd=0.2, tokens=0))
    assert violation == COST_CAP

    # Within cost budget, but 60 spent + 50 projected tokens exceeds the 100 token budget.
    violation = await state.check_budget(stored, BudgetProjection(cost_usd=0.05, tokens=50))
    assert violation == TOKEN_CAP


async def test_budget_check_accepts_and_charge_updates_totals(state) -> None:
    key = await _issue(state, max_budget_usd=1.0, max_tokens=1000)
    stored = await state.authenticate(key.secret)
    assert stored is not None

    projection = BudgetProjection(cost_usd=0.1, tokens=100)
    assert await state.check_budget(stored, projection) is None
    # The projection is reserved: a concurrent 0.95 request now exceeds the budget.
    assert await state.check_budget(stored, BudgetProjection(cost_usd=0.95, tokens=0)) == COST_CAP

    await state.charge(key.key_id, cost_usd=0.1, tokens=100)
    await state.release_budget(key.key_id, projection)

    updated = await state.key_store.key_by_id(key.key_id)
    assert updated is not None
    assert updated.spent_cost_usd == 0.1
    assert updated.spent_tokens == 100
    # After release, remaining budget is available again.
    assert await state.check_budget(stored, BudgetProjection(cost_usd=0.85, tokens=0)) is None


async def test_revoked_key_fails_budget_check(state) -> None:
    key = await _issue(state)
    stored = await state.authenticate(key.secret)
    assert stored is not None
    await state.revoke_key(key.key_id)
    assert await state.check_budget(stored, BudgetProjection(0.0, 0)) == COST_CAP


async def test_events_are_sequenced(state) -> None:
    key = await _issue(state)
    await state.revoke_key(key.key_id)
    events = state.events()
    assert [event["seq"] for event in events] == [1, 2]
    assert events[1]["type"] == EVENT_KEY_REVOKED
    assert state.events(after=1) == [events[1]]
