"""Tests for the async database abstraction and migrations (SQLite side)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import INTEGRITY_ERRORS, Database


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


async def test_migrations_create_all_tables(db: Database) -> None:
    tables = {
        str(row["name"])
        for row in await db.fetch("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "model_spec",
        "hf_artifact",
        "endpoint",
        "endpoint_ticket",
        "probe_report",
        "provider_instance",
        "host",
        "template_build",
        "sandbox_lease",
        "gateway_key",
        "cost_ledger",
        "trial",
        "grade",
        "round",
        "artifact",
        "incident",
        "canary",
        "schema_migration",
    } <= tables


async def test_migrate_is_idempotent(db: Database) -> None:
    assert await migrate(db) == []  # the fixture already applied everything


async def test_execute_fetch_and_placeholders(db: Database) -> None:
    await db.execute(
        "INSERT INTO host (host_id, instance, os, hw, hostd_version, state, last_heartbeat) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("host-1", "hostd-a", "macos", "arm64", "1.0.0", "active", "2026-11-06T12:00:00Z"),
    )
    row = await db.fetch_one("SELECT * FROM host WHERE host_id = %s", ("host-1",))
    assert row is not None
    assert row["state"] == "active"
    assert await db.fetch_one("SELECT * FROM host WHERE host_id = %s", ("missing",)) is None


async def test_transaction_commits_and_rolls_back(db: Database) -> None:
    params = ("host-1", "hostd-a", "macos", "arm64", "1.0.0", "active", "2026-11-06T12:00:00Z")
    insert = (
        "INSERT INTO host (host_id, instance, os, hw, hostd_version, state, last_heartbeat) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    async with db.transaction():
        await db.execute(insert, params)
        # Nested transactions join the outer one.
        async with db.transaction():
            await db.execute(insert, ("host-2", *params[1:]))
    assert len(await db.fetch("SELECT * FROM host")) == 2

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            await db.execute(insert, ("host-3", *params[1:]))
            raise RuntimeError("boom")
    assert await db.fetch_one("SELECT * FROM host WHERE host_id = %s", ("host-3",)) is None


async def test_integrity_errors_use_one_importable_tuple(db: Database) -> None:
    params = ("host-1", "hostd-a", "macos", "arm64", "1.0.0", "active", "2026-11-06T12:00:00Z")
    insert = (
        "INSERT INTO host (host_id, instance, os, hw, hostd_version, state, last_heartbeat) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    await db.execute(insert, params)
    with pytest.raises(INTEGRITY_ERRORS):
        await db.execute(insert, params)


async def test_unsupported_url_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported database URL"):
        await connect("mysql://example")
