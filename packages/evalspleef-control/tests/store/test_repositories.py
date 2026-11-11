"""Repository round-trip tests for every table (SQLite)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.store import (
    ArtifactRepository,
    CanaryRepository,
    EndpointRepository,
    EndpointTicketRepository,
    GatewayKeyRepository,
    GradeRepository,
    HfArtifactRepository,
    HostRepository,
    IncidentRepository,
    ModelSpecExistsError,
    ModelSpecRepository,
    ProbeReportRepository,
    ProviderInstanceRepository,
    RoundRepository,
    SandboxLeaseRepository,
    TemplateBuildRepository,
    TrialRepository,
    connect,
    migrate,
)
from evalspleef.control.store.db import Database
from evalspleef.control.store.rows import (
    ArtifactRow,
    CanaryRow,
    EndpointRow,
    EndpointTicketRow,
    GatewayKeyRow,
    GradeRow,
    HfArtifactRow,
    HostRow,
    IncidentRow,
    ModelSpecRow,
    ProbeReportRow,
    ProviderInstanceRow,
    RoundRow,
    SandboxLeaseRow,
    TemplateBuildRow,
    TrialRow,
)

NOW = "2026-11-06T12:00:00Z"
LATER = "2026-11-06T13:00:00Z"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


def spec_row(slug: str = "qwen3-32b", version: int = 1) -> ModelSpecRow:
    return ModelSpecRow(
        slug=slug,
        spec_version=version,
        family="qwen",
        roles=("target",),
        source={"vendor": "novita", "model_id": "qwen/qwen3-32b"},
        deployment={"adapter": "novita-dedicated", "account": "main"},
        declared_context_tokens=131072,
        declared_max_output_tokens=8192,
        pricing_ref="pricing/v3",
        license="apache-2.0",
        owner="eval-team",
        created_at=NOW,
    )


async def test_model_spec_is_immutable(db: Database) -> None:
    repo = ModelSpecRepository(db)
    await repo.insert(spec_row())
    fetched = await repo.get("qwen3-32b", 1)
    assert fetched == spec_row()
    # A changed spec is a new version, never an update of the old one.
    with pytest.raises(ModelSpecExistsError, match="immutable"):
        await repo.insert(spec_row())
    await repo.insert(spec_row(version=2))
    assert [row.spec_version for row in await repo.list_by_slug("qwen3-32b")] == [1, 2]


async def test_hf_artifact_upsert(db: Database) -> None:
    repo = HfArtifactRepository(db)
    row = HfArtifactRow(
        repo="Qwen/Qwen3-32B",
        revision="a1b2c3d",
        mirror_repo="evalspleef-mirror/qwen3-32b",
        files_sha256={"model.safetensors": "deadbeef"},
        safetensors_bytes=65_536,
        license="apache-2.0",
        verified_at=NOW,
    )
    await repo.upsert(row)
    await repo.upsert(row)  # upsert of the identical row is a no-op
    assert await repo.get("Qwen/Qwen3-32B", "a1b2c3d") == row


async def test_endpoint_projection_updates(db: Database) -> None:
    repo = EndpointRepository(db)
    row = EndpointRow(
        endpoint_id="ep_qwen3-32b_a1b2",
        model_slug="qwen3-32b",
        model_spec_version=1,
        adapter="novita-dedicated",
        account="main",
        state="registered",
        external_ids={},
        observed={},
        created_at=NOW,
    )
    await repo.upsert(row)
    await repo.upsert(row)
    await repo.update_state("ep_qwen3-32b_a1b2", "ready")
    fetched = await repo.get("ep_qwen3-32b_a1b2")
    assert fetched is not None and fetched.state == "ready"
    assert [r.endpoint_id for r in await repo.list_by_state("ready")] == ["ep_qwen3-32b_a1b2"]
    await repo.update_state("ep_qwen3-32b_a1b2", "retired", retired_at=LATER)
    assert (await repo.get("ep_qwen3-32b_a1b2")).retired_at == LATER  # type: ignore[union-attr]


async def test_endpoint_ticket_lifecycle(db: Database) -> None:
    repo = EndpointTicketRepository(db)
    row = EndpointTicketRow(
        ticket_id="tkt-1",
        endpoint_id="ep_qwen3-32b_a1b2",
        kind="deploy",
        instructions_md="1. open console",
        status="open",
        opened_at=NOW,
    )
    await repo.insert(row)
    await repo.update_status("tkt-1", "closed", operator="alice", closed_at=LATER)
    fetched = await repo.get("tkt-1")
    assert fetched is not None
    assert fetched.status == "closed"
    assert fetched.operator == "alice"
    assert [t.ticket_id for t in await repo.list_by_endpoint("ep_qwen3-32b_a1b2")] == ["tkt-1"]


async def test_probe_report_is_immutable(db: Database) -> None:
    repo = ProbeReportRepository(db)
    row = ProbeReportRow(
        report_id="prb-1",
        endpoint_id="ep_qwen3-32b_a1b2",
        gateway_version="gw-1.2.3",
        results={"tool_call": "pass"},
        capabilities={"streaming": True},
        created_at=NOW,
    )
    await repo.insert(row)
    assert await repo.get("prb-1") == row
    with pytest.raises(ValueError, match="already exists"):
        await repo.insert(row)


async def test_provider_instance_upsert(db: Database) -> None:
    repo = ProviderInstanceRepository(db)
    row = ProviderInstanceRow(
        instance="novita-sandbox/prod",
        adapter="novita-sandbox",
        version="2.1.3",
        capabilities={"os_families": ["linux"]},
        isolation_profile="profile: linux-microvm\n",
        verified_isolation=True,
        config_hash="sha256:abc",
    )
    await repo.upsert(row)
    await repo.upsert(row)
    assert await repo.get("novita-sandbox/prod") == row
    assert len(await repo.list_all()) == 1


async def test_host_upsert_and_state(db: Database) -> None:
    repo = HostRepository(db)
    row = HostRow(
        host_id="host-1",
        instance="hostd-a",
        os="macos",
        hw="arm64",
        hostd_version="1.0.0",
        state="active",
        last_heartbeat=NOW,
    )
    await repo.upsert(row)
    await repo.update_state("host-1", "draining", last_heartbeat=LATER)
    fetched = await repo.get("host-1")
    assert fetched is not None and fetched.state == "draining"
    assert fetched.last_heartbeat == LATER


async def test_template_build_upsert(db: Database) -> None:
    repo = TemplateBuildRepository(db)
    row = TemplateBuildRow(
        env_id="web-dev",
        env_version="1.0.0",
        oci_digest="sha256:1234",
        provider_template_ids={"novita-sandbox": "tpl-1"},
        fingerprints={"apt": "abc"},
        status="ready",
    )
    await repo.upsert(row)
    await repo.upsert(row)
    assert await repo.get("web-dev", "1.0.0") == row
    assert len(await repo.list_by_env("web-dev")) == 1


async def test_sandbox_lease_lifecycle(db: Database) -> None:
    repo = SandboxLeaseRepository(db)
    agent = SandboxLeaseRow(
        lease_id="lease-agent",
        batch_id="2026-11-b1",
        trial_id="web-001.m.s.e1.t01",
        role="agent",
        provider_instance="novita-sandbox/prod",
        host_id=None,
        template_kind="novita_template",
        template_ref="tpl-1",
        template_digest="sha256:9999",
        created_at=NOW,
    )
    verifier = SandboxLeaseRow(
        lease_id="lease-verifier",
        batch_id="2026-11-b1",
        trial_id="web-001.m.s.e1.t01",
        role="verifier",
        template_kind="novita_template",
        template_ref="tpl-2",
        template_digest="sha256:aaaa",
        created_at=NOW,
    )
    await repo.insert(agent)
    await repo.insert(verifier)
    assert len(await repo.list_by_trial("web-001.m.s.e1.t01")) == 2
    await repo.mark_destroyed("lease-agent", LATER, reaped=True)
    fetched = await repo.get("lease-agent")
    assert fetched is not None and fetched.destroyed_at == LATER and fetched.reaped is True


async def test_gateway_key_revoke(db: Database) -> None:
    keys = GatewayKeyRepository(db)
    row = GatewayKeyRow(
        key_id="key-1",
        trial_id="web-001.m.s.e1.t01",
        endpoint_id="ep_qwen3-32b_a1b2",
        budget={"usd": 5.0},
        expires_at=LATER,
    )
    await keys.insert(row)
    await keys.revoke("key-1", LATER)
    assert (await keys.get("key-1")).revoked_at == LATER  # type: ignore[union-attr]


async def test_trial_projection_row(db: Database) -> None:
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
    await repo.update_state("2026-11-b1", "web-001.m.s.e1.t01", "leased", updated_at=LATER)
    fetched = await repo.get("2026-11-b1", "web-001.m.s.e1.t01")
    assert fetched is not None and fetched.state == "leased" and fetched.validity == "pending"
    assert [t.trial_id for t in await repo.list_by_batch("2026-11-b1")] == ["web-001.m.s.e1.t01"]


async def test_trial_rows_are_scoped_by_batch(db: Database) -> None:
    """Two batches planned from the same config mint identical trial ids."""
    repo = TrialRepository(db)
    first = TrialRow(
        trial_id="web-001.m.s.e1.t01",
        batch_id="2026-11-b1",
        task_id="web-001",
        model="m",
        shell="s",
        state="scored",
        validity="valid",
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.upsert(first)
    # The same trial id under a second batch is a separate row, not an overwrite.
    await repo.upsert(
        TrialRow(
            trial_id="web-001.m.s.e1.t01",
            batch_id="2026-11-b2",
            task_id="web-001",
            model="m",
            shell="s",
            state="pending",
            validity="pending",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await repo.update_state("2026-11-b2", "web-001.m.s.e1.t01", "leased", updated_at=LATER)
    assert await repo.get("2026-11-b1", "web-001.m.s.e1.t01") == first
    reruns = {row.batch_id: row for row in await repo.list_by_batch("2026-11-b2")}
    assert reruns["2026-11-b2"].state == "leased"


async def test_grade_versions_append(db: Database) -> None:
    repo = GradeRepository(db)
    first = GradeRow(
        trial_id="web-001.m.s.e1.t01",
        batch_id="2026-11-b1",
        version=1,
        tier="B",
        status="final",
        decided_by="auto",
        payload={"rule": "G-F1-B"},
        created_at=NOW,
    )
    await repo.insert(first)
    await repo.insert(
        GradeRow(
            trial_id="web-001.m.s.e1.t01",
            batch_id="2026-11-b1",
            version=2,
            tier="A",
            status="final",
            decided_by="human",
            payload={"rule": "G-HUMAN-99", "supersedes": 1},
            created_at=LATER,
        )
    )
    assert [g.version for g in await repo.list_by_trial("2026-11-b1", "web-001.m.s.e1.t01")] == [
        1,
        2,
    ]
    current = await repo.current("2026-11-b1", "web-001.m.s.e1.t01")
    assert current is not None and current.tier == "A"
    with pytest.raises(ValueError, match="already exists"):
        await repo.insert(first)


async def test_grade_rows_are_scoped_by_batch(db: Database) -> None:
    """The same trial id in two batches gets its own grade versions."""
    repo = GradeRepository(db)
    for batch in ("2026-11-b1", "2026-11-b2"):
        await repo.insert(
            GradeRow(
                trial_id="web-001.m.s.e1.t01",
                batch_id=batch,
                version=1,
                tier="B",
                status="final",
                decided_by="auto",
                payload={"rule": "G-F1-B"},
                created_at=NOW,
            )
        )
        await repo.insert(
            GradeRow(
                trial_id="web-001.m.s.e1.t01",
                batch_id=batch,
                version=2,
                tier="A",
                status="final",
                decided_by="human",
                payload={"rule": "G-HUMAN-99", "supersedes": 1},
                created_at=LATER,
            )
        )
    for batch in ("2026-11-b1", "2026-11-b2"):
        versions = await repo.list_by_trial(batch, "web-001.m.s.e1.t01")
        assert [(g.version, g.tier) for g in versions] == [(1, "B"), (2, "A")]
        assert (await repo.current(batch, "web-001.m.s.e1.t01")).tier == "A"  # type: ignore[union-attr]


async def test_round_rows(db: Database) -> None:
    repo = RoundRepository(db)
    await repo.upsert(
        RoundRow(
            trial_id="web-001.m.s.e1.t01",
            batch_id="2026-11-b1",
            round_no=1,
            started_at=NOW,
            payload={},
        )
    )
    await repo.finish("2026-11-b1", "web-001.m.s.e1.t01", 1, stopped_at=LATER, agent_steps=12)
    rounds = await repo.list_by_trial("2026-11-b1", "web-001.m.s.e1.t01")
    assert len(rounds) == 1
    assert rounds[0].stopped_at == LATER and rounds[0].agent_steps == 12


async def test_round_rows_are_scoped_by_batch(db: Database) -> None:
    repo = RoundRepository(db)
    for batch in ("2026-11-b1", "2026-11-b2"):
        await repo.upsert(
            RoundRow(
                trial_id="web-001.m.s.e1.t01",
                batch_id=batch,
                round_no=1,
                started_at=NOW,
                payload={"batch": batch},
            )
        )
    for batch in ("2026-11-b1", "2026-11-b2"):
        (round_row,) = await repo.list_by_trial(batch, "web-001.m.s.e1.t01")
        assert round_row.payload == {"batch": batch}


async def test_artifact_index(db: Database) -> None:
    repo = ArtifactRepository(db)
    row = ArtifactRow(
        artifact_id="art-1",
        trial_id="web-001.m.s.e1.t01",
        kind="screenshot",
        path="traces/web-001/shot.png",
        sha256="cafe",
        size_bytes=2048,
        created_at=NOW,
    )
    await repo.insert(row)
    assert await repo.get("art-1") == row
    assert len(await repo.list_by_trial("web-001.m.s.e1.t01")) == 1


async def test_incident_open_close(db: Database) -> None:
    repo = IncidentRepository(db)
    row = IncidentRow(
        incident_id="inc-1",
        trial_id="web-001.m.s.e1.t01",
        error_class="environment",
        disposition="retry",
        detail={"host": "host-1"},
        opened_at=NOW,
    )
    await repo.insert(row)
    assert [i.incident_id for i in await repo.list_open()] == ["inc-1"]
    await repo.close("inc-1", LATER)
    assert await repo.list_open() == []
    assert (await repo.get("inc-1")).closed_at == LATER  # type: ignore[union-attr]


async def test_canary_scan(db: Database) -> None:
    repo = CanaryRepository(db)
    row = CanaryRow(
        canary_id="can-1",
        trial_id="web-001.m.s.e1.t01",
        token="token-secret",
        injected_at=NOW,
    )
    await repo.insert(row)
    await repo.mark_scanned("can-1", LATER, hits=0)
    fetched = await repo.get("can-1")
    assert fetched is not None and fetched.scanned_at == LATER and fetched.hits == 0
