"""Postgres-backed key and usage stores (psycopg 3, async)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import AsyncConnection

from evalspleef.spi.gateway import UsageRecord

from . import KeyStore, StoredKey, UsageStore
from .codec import (
    KEY_COLUMNS,
    USAGE_COLUMNS,
    insert_statement,
    key_from_row,
    key_params,
    usage_from_row,
    usage_params,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    key_id TEXT PRIMARY KEY,
    secret_sha256 TEXT UNIQUE NOT NULL,
    trial_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    max_budget_usd DOUBLE PRECISION NOT NULL,
    max_tokens BIGINT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    spent_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    spent_tokens BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    seq BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    round BIGINT NOT NULL,
    endpoint_id TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    input_tokens BIGINT NOT NULL,
    output_tokens BIGINT NOT NULL,
    cached_tokens BIGINT NOT NULL,
    reasoning_tokens BIGINT NOT NULL,
    latency_ms BIGINT NOT NULL,
    status_code BIGINT NOT NULL,
    cold_start_ms BIGINT NOT NULL,
    cost_usd DOUBLE PRECISION NOT NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS usage_trial_idx ON usage (trial_id, seq);
"""


class PostgresStore(KeyStore, UsageStore):
    """psycopg 3 async connection in autocommit mode; one statement is one transaction."""

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    @classmethod
    async def create(cls, url: str) -> PostgresStore:
        connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
        async with connection.cursor() as cursor:
            await cursor.execute(_SCHEMA)
            await cursor.execute("ALTER TABLE usage ADD COLUMN IF NOT EXISTS error_code TEXT")
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def insert_key(self, key: StoredKey) -> None:
        statement = insert_statement("keys", KEY_COLUMNS, "format")
        async with self._connection.cursor() as cursor:
            await cursor.execute(statement, key_params(key))

    async def key_by_secret_hash(self, secret_sha256: str) -> StoredKey | None:
        async with self._connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT {KEY_COLUMNS} FROM keys WHERE secret_sha256 = %s", (secret_sha256,)
            )
            row = await cursor.fetchone()
            return key_from_row(row) if row is not None else None

    async def key_by_id(self, key_id: str) -> StoredKey | None:
        async with self._connection.cursor() as cursor:
            await cursor.execute(f"SELECT {KEY_COLUMNS} FROM keys WHERE key_id = %s", (key_id,))
            row = await cursor.fetchone()
            return key_from_row(row) if row is not None else None

    async def set_revoked(self, key_id: str) -> None:
        async with self._connection.cursor() as cursor:
            await cursor.execute("UPDATE keys SET revoked = TRUE WHERE key_id = %s", (key_id,))

    async def add_spend(self, key_id: str, *, cost_usd: float, tokens: int) -> StoredKey:
        async with self._connection.cursor() as cursor:
            await cursor.execute(
                "UPDATE keys SET spent_cost_usd = spent_cost_usd + %s,"
                " spent_tokens = spent_tokens + %s WHERE key_id = %s"
                " RETURNING " + KEY_COLUMNS,
                (cost_usd, tokens, key_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(key_id)
            return key_from_row(row)

    async def append(self, record: UsageRecord) -> None:
        statement = insert_statement("usage", USAGE_COLUMNS, "format")
        async with self._connection.cursor() as cursor:
            await cursor.execute(statement, usage_params(record))

    async def for_trial(self, trial_id: str) -> list[UsageRecord]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT {USAGE_COLUMNS} FROM usage WHERE trial_id = %s ORDER BY seq",
                (trial_id,),
            )
            rows = await cursor.fetchall()
            return [usage_from_row(row) for row in rows]
