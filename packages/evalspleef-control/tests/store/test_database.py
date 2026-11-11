"""Tests for the async database abstraction and migrations (SQLite side)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import INTEGRITY_ERRORS, Database
from evalspleef.control.store.migrations import MIGRATIONS_DIR
from evalspleef.control.store.repos import GradeRepository, TrialRepository

NOW = "2026-11-06T12:00:00Z"
TRIAL = "web-001.m.s.e1.t01"


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


@pytest_asyncio.fixture
async def old_store(tmp_path: Path) -> AsyncIterator[Database]:
    """A store frozen at migration 0002, the schema before batch-scoped trial keys."""
    database = await connect(f"sqlite:///{tmp_path}/old.sqlite3")
    await database.execute(
        "CREATE TABLE schema_migration (name TEXT NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for name in ("0001_init.sql", "0002_endpoint_benchmark.sql"):
        # Apply the old migration files with the same statement splitting migrate() uses.
        script = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        statement: list[str] = []
        for line in script.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            statement.append(line)
            if stripped.endswith(";"):
                await database.execute("\n".join(statement).removesuffix(";"))
                statement = []
        await database.execute(
            "INSERT INTO schema_migration (name, applied_at) VALUES (%s, %s)", (name, NOW)
        )
    yield database
    await database.close()


async def test_batch_scoped_keys_migration_keeps_existing_rows_readable(
    old_store: Database,
) -> None:
    """Rows written under the old trial_id-only keys survive the 0003 rebuild."""
    await old_store.execute(
        "INSERT INTO trial (trial_id, batch_id, cell, task_id, model, shell, state, validity, "
        "epoch, idx, extensions, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (TRIAL, "2026-11-b1", None, "web-001", "m", "s", "scored", "valid", 1, 1, "{}", NOW, NOW),
    )
    await old_store.execute(
        "INSERT INTO grade (trial_id, version, tier, status, decided_by, rule_id, payload, "
        "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (TRIAL, 1, "A", "final", "auto", None, "{}", NOW),
    )

    applied = await migrate(old_store)

    assert applied == ["0003_batch_scoped_trial_keys.sql"]
    row = await TrialRepository(old_store).get("2026-11-b1", TRIAL)
    assert row is not None and (row.state, row.validity) == ("scored", "valid")
    (grade,) = await GradeRepository(old_store).list_by_trial("2026-11-b1", TRIAL)
    assert (grade.version, grade.tier, grade.batch_id) == (1, "A", "2026-11-b1")
