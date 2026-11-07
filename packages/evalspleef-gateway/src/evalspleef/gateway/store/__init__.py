"""Persistence for gateway keys and usage records.

Two protocols, :class:`KeyStore` and :class:`UsageStore`, with three implementations:

- :class:`~evalspleef.gateway.store.memory.MemoryStore` — process-local dicts;
- :class:`~evalspleef.gateway.store.sqlite.SqliteStore` — stdlib ``sqlite3`` on a file,
  blocking calls run in a thread executor;
- :class:`~evalspleef.gateway.store.postgres.PostgresStore` — psycopg 3 async.

:func:`open_stores` selects an implementation from a URL: ``memory://``,
``sqlite:///<path>`` or ``postgresql://...``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from evalspleef.spi.gateway import UsageRecord

__all__ = ["KeyStore", "MemoryStore", "StoredKey", "UsageStore", "open_stores"]


@dataclass(frozen=True)
class StoredKey:
    """A trial key as persisted. The plaintext secret never appears here, only its hash."""

    key_id: str
    secret_sha256: str
    trial_id: str
    endpoint_id: str
    max_budget_usd: float
    max_tokens: int
    expires_at: str
    revoked: bool = False
    spent_cost_usd: float = 0.0
    spent_tokens: int = 0


class KeyStore:
    """Async persistence for trial keys."""

    async def insert_key(self, key: StoredKey) -> None:
        """Persist a freshly issued key."""
        raise NotImplementedError

    async def key_by_secret_hash(self, secret_sha256: str) -> StoredKey | None:
        """Look a key up by the SHA-256 hash of its plaintext secret."""
        raise NotImplementedError

    async def key_by_id(self, key_id: str) -> StoredKey | None:
        raise NotImplementedError

    async def set_revoked(self, key_id: str) -> None:
        raise NotImplementedError

    async def add_spend(self, key_id: str, *, cost_usd: float, tokens: int) -> StoredKey:
        """Atomically add spent cost and tokens; returns the updated key.

        Raises ``KeyError`` when the key does not exist.
        """
        raise NotImplementedError


class UsageStore:
    """Async persistence for per-request usage records."""

    async def append(self, record: UsageRecord) -> None:
        raise NotImplementedError

    async def for_trial(self, trial_id: str) -> list[UsageRecord]:
        raise NotImplementedError


async def open_stores(url: str) -> tuple[KeyStore, UsageStore]:
    """Open the key and usage stores selected by ``url``."""
    if url == "memory://":
        store: KeyStore | UsageStore = MemoryStore()
    elif url.startswith("sqlite:///"):
        from .sqlite import SqliteStore

        store = SqliteStore(url[len("sqlite:///") :])
    elif url.startswith("postgresql://"):
        from .postgres import PostgresStore

        store = await PostgresStore.create(url)
    else:
        raise ValueError(f"unsupported store URL: {url!r}")
    key_store = cast(KeyStore, store)
    usage_store = cast(UsageStore, store)
    return key_store, usage_store


from .memory import MemoryStore  # noqa: E402  (re-exported; import after the protocols above)
