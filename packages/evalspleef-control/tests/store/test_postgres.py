"""PostgreSQL tests against a throw-away real container.

Spins up ``postgres:16`` on a random host port with the docker CLI, waits for a
real connection inside a bounded readiness loop, runs the migration and CRUD
checks against the real backend, and always removes the container afterwards.
When the Docker daemon is unreachable the module skips with an explicit
message naming Docker.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio

from evalspleef.control.store import (
    GradeRepository,
    GradeRow,
    ModelSpecExistsError,
    ModelSpecRepository,
    ModelSpecRow,
    TrialRepository,
    TrialRow,
    connect,
    migrate,
)
from evalspleef.control.store.db import PostgresDatabase

POSTGRES_IMAGE = "postgres:16"
PULL_TIMEOUT_S = 600
POSTGRES_PASSWORD = "spleef-test"
READINESS_TIMEOUT_S = 180
CONNECT_TIMEOUT_S = 5
BACKOFF_S = 1.0
NOW = "2026-11-06T12:00:00Z"


def _docker_available() -> tuple[bool, str]:
    """Check the Docker daemon, returning (available, diagnostic message)."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker CLI unreachable: {exc}"
    if result.returncode != 0:
        return False, f"docker daemon unreachable: {result.stderr.strip() or result.stdout.strip()}"
    return True, f"docker server {result.stdout.strip()}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spec_row(slug: str = "qwen3-32b", version: int = 1) -> ModelSpecRow:
    return ModelSpecRow(
        slug=slug,
        spec_version=version,
        family="qwen",
        roles=("target",),
        source={"vendor": "novita"},
        deployment={"adapter": "novita-dedicated"},
        declared_context_tokens=131072,
        declared_max_output_tokens=8192,
        pricing_ref="pricing/v3",
        license="apache-2.0",
        owner="eval-team",
        created_at=NOW,
    )


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """Start postgres:16, yield a connection URL, remove the container in finally."""
    available, message = _docker_available()
    if not available:
        pytest.skip(f"Docker not usable for the PostgreSQL test: {message}")

    # Pull first, under its own deadline: on a machine without the image the pull alone can take
    # longer than starting the container is allowed to.
    pull = subprocess.run(
        ["docker", "pull", "--quiet", POSTGRES_IMAGE],
        capture_output=True,
        text=True,
        timeout=PULL_TIMEOUT_S,
        check=False,
    )
    if pull.returncode != 0:
        pytest.skip(f"Docker could not pull {POSTGRES_IMAGE}: {pull.stderr.strip()}")
    container = f"evalspleef-pg-test-{uuid.uuid4().hex[:12]}"
    port = _free_port()
    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--name",
            container,
            "--env",
            f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
            "--env",
            "POSTGRES_USER=postgres",
            "--env",
            "POSTGRES_DB=spleef",
            "--publish",
            f"127.0.0.1:{port}:5432",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"Docker could not start {POSTGRES_IMAGE}: {run.stderr.strip()}")

    url = f"postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{port}/spleef"
    try:
        # Bounded readiness loop on a real connection, one second between tries.
        deadline = time.monotonic() + READINESS_TIMEOUT_S
        while True:
            try:
                conn = psycopg.Connection.connect(url, connect_timeout=CONNECT_TIMEOUT_S)
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail(f"postgres:16 not ready within {READINESS_TIMEOUT_S}s")
                time.sleep(BACKOFF_S)
                continue
            conn.close()
            break
        yield url
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", container],
            capture_output=True,
            timeout=60,
            check=False,
        )


@pytest_asyncio.fixture
async def db(postgres_url: str) -> AsyncIterator[PostgresDatabase]:
    database = await connect(postgres_url)
    assert isinstance(database, PostgresDatabase)
    await migrate(database)
    yield database
    await database.close()


async def test_migrations_create_schema(db: PostgresDatabase) -> None:
    rows = await db.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    tables = {str(row["table_name"]) for row in rows}
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


async def test_model_spec_immutability_on_postgres(db: PostgresDatabase) -> None:
    repo = ModelSpecRepository(db)
    await repo.insert(_spec_row())
    fetched = await repo.get("qwen3-32b", 1)
    assert fetched is not None
    assert fetched.roles == ("target",)
    assert fetched.source == {"vendor": "novita"}
    with pytest.raises(ModelSpecExistsError):
        await repo.insert(_spec_row())


async def test_trial_roundtrip_and_transaction_rollback(db: PostgresDatabase) -> None:
    repo = TrialRepository(db)
    row = TrialRow(
        trial_id="web-001.m.s.e1.t01",
        batch_id="2026-11-b1",
        task_id="web-001",
        model="m",
        shell="s",
        state="pending",
        validity="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.upsert(row)
    await repo.update_state("2026-11-b1", "web-001.m.s.e1.t01", "leased", updated_at=NOW)
    fetched = await repo.get("2026-11-b1", "web-001.m.s.e1.t01")
    assert fetched is not None and fetched.state == "leased"

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            await repo.update_state("2026-11-b1", "web-001.m.s.e1.t01", "scored")
            raise RuntimeError("boom")
    rolled_back = await repo.get("2026-11-b1", "web-001.m.s.e1.t01")
    assert rolled_back is not None and rolled_back.state == "leased"


async def test_two_batches_with_the_same_config_coexist(db: PostgresDatabase) -> None:
    """Identical trial ids under two batches stay separate rows (NFR-01 rerun stability)."""
    trials = TrialRepository(db)
    grades = GradeRepository(db)
    for batch in ("2026-11-b1", "2026-11-b2"):
        await trials.upsert(
            TrialRow(
                trial_id="web-001.m.s.e1.t01",
                batch_id=batch,
                task_id="web-001",
                model="m",
                shell="s",
                state="scored",
                validity="valid",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await grades.insert(
            GradeRow(
                trial_id="web-001.m.s.e1.t01",
                batch_id=batch,
                version=1,
                tier="A",
                status="final",
                decided_by="auto",
                payload={"rule": "G-FIXTURE"},
                created_at=NOW,
            )
        )
    assert len(await trials.list_by_batch("2026-11-b1")) == 1
    assert len(await trials.list_by_batch("2026-11-b2")) == 1
    assert (await trials.get("2026-11-b1", "web-001.m.s.e1.t01")).batch_id == "2026-11-b1"  # type: ignore[union-attr]
    for batch in ("2026-11-b1", "2026-11-b2"):
        versions = await grades.list_by_trial(batch, "web-001.m.s.e1.t01")
        assert [(g.version, g.tier) for g in versions] == [(1, "A")]
