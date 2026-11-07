"""Round-trip tests for the memory and sqlite stores; postgres against a real container."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from evalspleef.gateway.store import StoredKey, open_stores
from evalspleef.gateway.store.memory import MemoryStore
from evalspleef.gateway.store.sqlite import SqliteStore
from evalspleef.spi.gateway import UsageRecord

_RECORD = UsageRecord(
    ts="2026-01-01T00:00:00+00:00",
    trial_id="trial-1",
    round=2,
    endpoint_id="ep1",
    upstream_model="fake-model",
    input_tokens=10,
    output_tokens=5,
    cached_tokens=3,
    reasoning_tokens=1,
    latency_ms=42,
    status_code=200,
    cold_start_ms=0,
    cost_usd=0.001,
)

_KEY = StoredKey(
    key_id="tk_1",
    secret_sha256="ab" * 32,
    trial_id="trial-1",
    endpoint_id="ep1",
    max_budget_usd=1.0,
    max_tokens=100,
    expires_at="2099-01-01T00:00:00Z",
)


async def _roundtrip(key_store, usage_store) -> None:
    await key_store.insert_key(_KEY)
    got = await key_store.key_by_secret_hash(_KEY.secret_sha256)
    assert got == _KEY
    got_id = await key_store.key_by_id("tk_1")
    assert got_id == _KEY

    updated = await key_store.add_spend("tk_1", cost_usd=0.5, tokens=60)
    assert updated.spent_cost_usd == 0.5
    assert updated.spent_tokens == 60

    await key_store.set_revoked("tk_1")
    revoked = await key_store.key_by_id("tk_1")
    assert revoked is not None and revoked.revoked

    await usage_store.append(_RECORD)
    await usage_store.append(replace(_RECORD, trial_id="trial-2"))
    records = await usage_store.for_trial("trial-1")
    assert records == [_RECORD]


async def test_memory_store_roundtrip() -> None:
    store = MemoryStore()
    await _roundtrip(store, store)


async def test_sqlite_store_roundtrip(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "gw.db"))
    await _roundtrip(store, store)


async def test_open_stores_selects_implementation(tmp_path: Path) -> None:
    memory_key, _memory_usage = await open_stores("memory://")
    assert isinstance(memory_key, MemoryStore)
    sqlite_key, _ = await open_stores(f"sqlite:///{tmp_path / 'x.db'}")
    assert isinstance(sqlite_key, SqliteStore)
    with pytest.raises(ValueError, match="unsupported store URL"):
        await open_stores("etcd://nope")


# -- postgres against a real throw-away container -------------------------------------


def _docker_available() -> str | None:
    """Return a skip reason when Docker cannot run containers, else ``None``."""
    if shutil.which("docker") is None:
        return "Docker is unavailable: the docker CLI is not on PATH"
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return f"Docker is unavailable: docker info failed: {result.stderr.strip()[:200]}"
    return None


_SKIP_REASON = _docker_available()


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "Docker unavailable")
async def test_postgres_store_roundtrip() -> None:
    database = f"gw_{uuid.uuid4().hex[:12]}"
    password = "spleef-test"
    container = f"spleef-gw-test-{uuid.uuid4().hex[:8]}"
    # Docker CLI calls block briefly; the container lifetime far exceeds that.
    subprocess.run(  # noqa: ASYNC221
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_USER=spleef",
            "-p",
            "127.0.0.1::5432",
            "postgres:16",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        port = _published_port(container)
        dsn = f"postgresql://spleef:{password}@127.0.0.1:{port}/{database}"
        admin_dsn = f"postgresql://spleef:{password}@127.0.0.1:{port}/postgres"
        await _wait_ready(admin_dsn)
        await _create_database(admin_dsn, database)
        key_store, usage_store = await open_stores(dsn)
        await _roundtrip(key_store, usage_store)
    finally:
        subprocess.run(  # noqa: ASYNC221
            ["docker", "rm", "-f", container],
            capture_output=True,
            timeout=60,
        )


def _published_port(container: str) -> int:
    result = subprocess.run(
        ["docker", "port", container, "5432/tcp"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    # 127.0.0.1:49153
    return int(result.stdout.strip().rsplit(":", 1)[1])


async def _wait_ready(dsn: str) -> None:
    """Bounded retry loop on a real connection until postgres accepts queries."""
    import psycopg

    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = await psycopg.AsyncConnection.connect(dsn, connect_timeout=3)
            await connection.close()
            return
        except Exception as exc:  # postgres is still starting up
            last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"postgres container did not become ready: {last_error}")


async def _create_database(admin_dsn: str, database: str) -> None:
    import psycopg

    connection = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()
