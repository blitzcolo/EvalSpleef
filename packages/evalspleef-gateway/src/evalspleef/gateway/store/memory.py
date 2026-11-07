"""In-memory key and usage stores."""

from __future__ import annotations

import threading
from dataclasses import replace

from evalspleef.spi.gateway import UsageRecord

from . import KeyStore, StoredKey, UsageStore


class MemoryStore(KeyStore, UsageStore):
    """Process-local stores. Thread-safe; async methods do not block."""

    def __init__(self) -> None:
        self._keys_by_id: dict[str, StoredKey] = {}
        self._key_ids_by_hash: dict[str, str] = {}
        self._usage: list[UsageRecord] = []
        self._lock = threading.Lock()

    async def insert_key(self, key: StoredKey) -> None:
        with self._lock:
            self._keys_by_id[key.key_id] = key
            self._key_ids_by_hash[key.secret_sha256] = key.key_id

    async def key_by_secret_hash(self, secret_sha256: str) -> StoredKey | None:
        with self._lock:
            key_id = self._key_ids_by_hash.get(secret_sha256)
            return self._keys_by_id.get(key_id) if key_id is not None else None

    async def key_by_id(self, key_id: str) -> StoredKey | None:
        with self._lock:
            return self._keys_by_id.get(key_id)

    async def set_revoked(self, key_id: str) -> None:
        with self._lock:
            key = self._keys_by_id.get(key_id)
            if key is not None:
                self._keys_by_id[key_id] = replace(key, revoked=True)

    async def add_spend(self, key_id: str, *, cost_usd: float, tokens: int) -> StoredKey:
        with self._lock:
            key = self._keys_by_id.get(key_id)
            if key is None:
                raise KeyError(key_id)
            updated = replace(
                key,
                spent_cost_usd=key.spent_cost_usd + cost_usd,
                spent_tokens=key.spent_tokens + tokens,
            )
            self._keys_by_id[key_id] = updated
            return updated

    async def append(self, record: UsageRecord) -> None:
        with self._lock:
            self._usage.append(record)

    async def for_trial(self, trial_id: str) -> list[UsageRecord]:
        with self._lock:
            return [record for record in self._usage if record.trial_id == trial_id]
