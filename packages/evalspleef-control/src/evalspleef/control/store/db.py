"""Async relational database abstraction: SQLite via a thread executor, PostgreSQL via psycopg 3.

The control plane's relational store (TECH-DESIGN section 11) speaks one
portable SQL dialect through this interface:

* placeholders are always ``%s`` (psycopg style); the SQLite adapter rewrites
  them to qmark,
* JSON documents travel as TEXT columns, encoded/decoded by the repositories,
* booleans are stored as INTEGER 0/1,
* timestamps are ISO-8601 UTC strings.

Open a database with :func:`connect` using ``sqlite:///<path>`` (use
``sqlite:///:memory:`` for tests) or ``postgresql://...`` URLs, then run
:func:`evalspleef.control.store.migrate` to create the schema.
"""

from __future__ import annotations

import asyncio
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

__all__ = [
    "INTEGRITY_ERRORS",
    "Database",
    "PostgresDatabase",
    "SqliteDatabase",
    "connect",
]

#: Raised by both backends when a constraint (e.g. a primary key) is violated.
INTEGRITY_ERRORS: tuple[type[Exception], ...] = (sqlite3.IntegrityError, psycopg.IntegrityError)

#: A result row addressed by column name.
Row = dict[str, Any]

_DEFAULT_STATEMENT_TIMEOUT_S = 60.0


class Database(ABC):
    """One async database connection with a small portable-SQL surface."""

    @abstractmethod
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run a statement that returns no rows."""

    @abstractmethod
    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Run a query and return all rows as dicts."""

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        """Run a query and return the first row, or None when the result is empty."""
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    @abstractmethod
    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Run statements atomically; rolls back on any exception."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection and release background resources."""


async def connect(url: str, *, timeout_s: float = _DEFAULT_STATEMENT_TIMEOUT_S) -> Database:
    """Open a :class:`Database` from a URL.

    ``sqlite:///<path>`` (``sqlite:///:memory:`` for an in-memory database) or
    ``postgresql://[<user>[:<password>]@]<host>[:<port>]/<dbname>``.
    """
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        return SqliteDatabase(path)
    if url.startswith("postgresql://"):
        conn = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(url, row_factory=dict_row, autocommit=True),
            timeout=timeout_s,
        )
        return PostgresDatabase(conn)
    raise ValueError(f"unsupported database URL (expected sqlite:/// or postgresql://): {url!r}")


class SqliteDatabase(Database):
    """SQLite connection driven from a dedicated single-thread executor.

    ``sqlite3`` connections are not asyncio-aware, so every call runs in a
    thread pool (``asyncio.to_thread`` would risk interleaving transactions on
    the shared connection; the single worker serializes access instead). The
    asyncio lock additionally serializes coroutines: operations inside an
    explicit :meth:`transaction` own the connection and run unguarded.
    """

    def __init__(self, path: str, *, executor: ThreadPoolExecutor | None = None) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="evalspleef-sqlite"
        )
        self._owns_executor = executor is None
        target = ":memory:" if path in {":memory:", ""} else path
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[Any] | None = None

    @staticmethod
    def _adapt(sql: str) -> str:
        """Translate psycopg-style placeholders to sqlite qmark placeholders."""
        return sql.replace("%s", "?")

    async def _run(self, fn: Any, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    @asynccontextmanager
    async def _op_guard(self) -> AsyncIterator[None]:
        if self._tx_owner is asyncio.current_task():
            yield
            return
        async with self._lock:
            yield

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._op_guard():
            await self._run(self._conn.execute, self._adapt(sql), tuple(params))
            if self._tx_owner is None:
                await self._run(self._conn.commit)

    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._op_guard():
            cursor = await self._run(self._conn.execute, self._adapt(sql), tuple(params))
            rows: list[Row] = await self._run(self._rows_to_dicts, cursor)
            return rows

    @staticmethod
    def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[Row]:
        return [dict(row) for row in cursor.fetchall()]

    def transaction(self) -> AbstractAsyncContextManager[None]:
        @asynccontextmanager
        async def _transaction() -> AsyncIterator[None]:
            task = asyncio.current_task()
            if self._tx_owner is task:
                yield  # nested transaction: join the outer one
                return
            async with self._lock:
                self._tx_owner = task
                try:
                    await self._run(self._conn.execute, "BEGIN")
                    yield
                except BaseException:
                    await self._run(self._conn.rollback)
                    raise
                else:
                    await self._run(self._conn.commit)
                finally:
                    self._tx_owner = None

        return _transaction()

    async def close(self) -> None:
        async with self._lock:
            await self._run(self._conn.close)
            if self._owns_executor:
                self._executor.shutdown(wait=True)


class PostgresDatabase(Database):
    """PostgreSQL connection via psycopg 3 (async API, dict rows, autocommit).

    The connection runs in autocommit mode; :meth:`transaction` uses psycopg's
    ``transaction()`` context, which issues BEGIN/COMMIT (or ROLLBACK) around
    the block. An asyncio lock serializes concurrent coroutines, mirroring the
    SQLite adapter.
    """

    def __init__(self, conn: psycopg.AsyncConnection[Row]) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[Any] | None = None
        self._closed = False

    @property
    def raw_connection(self) -> psycopg.AsyncConnection[Row]:
        """The underlying psycopg connection (escape hatch for diagnostics)."""
        return self._conn

    @asynccontextmanager
    async def _op_guard(self) -> AsyncIterator[None]:
        if self._tx_owner is asyncio.current_task():
            yield
            return
        async with self._lock:
            if self._closed:
                raise RuntimeError("database connection is closed")
            yield

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._op_guard():
            await self._conn.execute(sql, tuple(params))

    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._op_guard():
            cursor = await self._conn.execute(sql, tuple(params))
            return [dict(row) for row in await cursor.fetchall()]

    def transaction(self) -> AbstractAsyncContextManager[None]:
        @asynccontextmanager
        async def _transaction() -> AsyncIterator[None]:
            task = asyncio.current_task()
            if self._tx_owner is task:
                yield
                return
            async with self._lock:
                self._tx_owner = task
                try:
                    async with self._conn.transaction():
                        yield
                finally:
                    self._tx_owner = None

        return _transaction()

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                await self._conn.close()
