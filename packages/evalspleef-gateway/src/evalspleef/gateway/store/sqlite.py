"""SQLite-backed key and usage stores (stdlib ``sqlite3``, blocking calls in a thread)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, TypeVar

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
    max_budget_usd REAL NOT NULL,
    max_tokens INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    spent_cost_usd REAL NOT NULL DEFAULT 0,
    spent_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    endpoint_id TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    cold_start_ms INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_trial_idx ON usage (trial_id, seq);
"""

_T = TypeVar("_T")


class SqliteStore(KeyStore, UsageStore):
    """One connection guarded by a lock; every call runs in the default thread executor."""

    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._lock = threading.Lock()

    async def _run(self, fn: Callable[[], _T]) -> _T:
        return await asyncio.to_thread(self._locked, fn)

    def _locked(self, fn: Callable[[], _T]) -> _T:
        with self._lock:
            return fn()

    async def insert_key(self, key: StoredKey) -> None:
        statement = insert_statement("keys", KEY_COLUMNS, "qmark")

        def op() -> None:
            self._connection.execute(statement, key_params(key))
            self._connection.commit()

        await self._run(op)

    async def key_by_secret_hash(self, secret_sha256: str) -> StoredKey | None:
        def op() -> StoredKey | None:
            row = self._connection.execute(
                f"SELECT {KEY_COLUMNS} FROM keys WHERE secret_sha256 = ?", (secret_sha256,)
            ).fetchone()
            return key_from_row(row) if row is not None else None

        return await self._run(op)

    async def key_by_id(self, key_id: str) -> StoredKey | None:
        def op() -> StoredKey | None:
            row = self._connection.execute(
                f"SELECT {KEY_COLUMNS} FROM keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            return key_from_row(row) if row is not None else None

        return await self._run(op)

    async def set_revoked(self, key_id: str) -> None:
        def op() -> None:
            self._connection.execute("UPDATE keys SET revoked = 1 WHERE key_id = ?", (key_id,))
            self._connection.commit()

        await self._run(op)

    async def add_spend(self, key_id: str, *, cost_usd: float, tokens: int) -> StoredKey:
        def op() -> StoredKey:
            row = self._connection.execute(
                "UPDATE keys SET spent_cost_usd = spent_cost_usd + ?, spent_tokens = spent_tokens + ?"
                " WHERE key_id = ? RETURNING " + KEY_COLUMNS,
                (cost_usd, tokens, key_id),
            ).fetchone()
            if row is None:
                raise KeyError(key_id)
            self._connection.commit()
            return key_from_row(row)

        return await self._run(op)

    async def append(self, record: UsageRecord) -> None:
        statement = insert_statement("usage", USAGE_COLUMNS, "qmark")

        def op() -> None:
            self._connection.execute(statement, usage_params(record))
            self._connection.commit()

        await self._run(op)

    async def for_trial(self, trial_id: str) -> list[UsageRecord]:
        def op() -> list[UsageRecord]:
            rows: list[Any] = self._connection.execute(
                f"SELECT {USAGE_COLUMNS} FROM usage WHERE trial_id = ? ORDER BY seq",
                (trial_id,),
            ).fetchall()
            return [usage_from_row(row) for row in rows]

        return await self._run(op)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
