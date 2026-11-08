"""Operator actions on trials the real runner finished: regrade, void, resolve."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from evalspleef.control.batch.operations import OperationRefused, TrialOperations
from evalspleef.control.events import verify_projection
from evalspleef.control.store import TrialRepository, TrialRow, connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.control.store.repos import GradeRepository
from evalspleef.control.store.rows import GradeRow
from evalspleef.control.trial.testing import make_result
from evalspleef.core.events import EventEnvelope, EventType

BATCH = "2026-11-b1"
TRIAL_ID = "backend-001.demo-model.scripted.e1.t01"
NOW = "2026-11-08T09:00:00Z"


def lease_events() -> list[EventEnvelope]:
    """What the driver and the lease manager append before the runner starts."""
    common = {"ts": NOW, "batch_id": BATCH, "trial_id": TRIAL_ID}
    lease = {"event": "lease", "from": "pending", "to": "leased"}
    return [
        EventEnvelope(
            event_id="ev_op_created", type=EventType.TRIAL_CREATED, actor="d", payload={}, **common
        ),
        EventEnvelope(
            event_id="ev_op_leased",
            type=EventType.TRIAL_LEASED,
            actor="l",
            payload={"transition": lease},
            **common,
        ),
    ]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


async def finished(h: Any, db: Database) -> TrialOperations:
    """Run the trial and project it like the driver does: lease events, trial row, grade rows."""
    for event in lease_events():
        await h.events.append(event)
    outcome = await h.run()
    record = outcome.record or {}
    trials, grades = TrialRepository(db), GradeRepository(db)
    await trials.upsert(
        TrialRow(
            trial_id=TRIAL_ID,
            batch_id=BATCH,
            task_id="backend-001",
            model="demo-model",
            shell="scripted",
            state=str(record.get("state")),
            validity=str(record.get("validity")),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for grade in record.get("grades") or ():
        await grades.insert(
            GradeRow(
                trial_id=TRIAL_ID,
                version=int(grade["grade_version"]),
                tier=str(grade["tier"]),
                status=str(grade["status"]),
                decided_by=str(grade["decided_by"]),
                payload=dict(grade),
                created_at=NOW,
                rule_id=grade["decision"]["rule_id"],
            )
        )
    return TrialOperations(events=h.events, trials=trials, grades=grades, now=lambda: NOW)


async def assert_projected(h: Any, db: Database) -> None:
    report = await verify_projection(h.events, db, batch_id=BATCH)
    assert report.ok, report.mismatches


async def test_regrade_appends_an_identical_version_from_the_recorded_inputs(
    harness_factory: Any, rnd: Any, db: Database
) -> None:
    h = await harness_factory(rounds=[rnd()])
    ops = await finished(h, db)

    regraded = await ops.regrade(TRIAL_ID, reason="verifier image patched")

    versions = await ops.grades.list_by_trial(TRIAL_ID)
    assert [g.version for g in versions] == [1, 2]
    assert regraded.payload["supersedes"] == 1
    # Grading is a pure function of the recorded inputs: same tier, penalty and rule.
    for key in ("tier", "penalty", "decision"):
        assert regraded.payload[key] == versions[0].payload[key]
    assert (await h.of_type("grade.computed"))[-1].payload["regrade"] is True
    await assert_projected(h, db)


async def test_void_takes_a_scored_trial_out_post_hoc(
    harness_factory: Any, rnd: Any, db: Database
) -> None:
    h = await harness_factory(rounds=[rnd()])
    ops = await finished(h, db)

    assert str(await ops.void(TRIAL_ID, reason="task bug found after release")) == "invalid"

    row = await ops.trials.get(TRIAL_ID)
    assert row is not None and (row.state, row.validity) == ("invalid", "invalid")
    (notice,) = await h.of_type("trial.invalidated")
    assert notice.payload["transition"]["event"] == "invalidate_post_hoc"
    await assert_projected(h, db)
    with pytest.raises(OperationRefused):
        await ops.void(TRIAL_ID, reason="again")
    with pytest.raises(OperationRefused):
        await ops.regrade(TRIAL_ID, reason="an invalid trial has no grade to redo")


async def test_resolve_scores_a_paused_trial_with_the_human_grade(
    harness_factory: Any, rnd: Any, db: Database
) -> None:
    final = make_result(manual_reasons=["the judge returned no usable verdict"])
    h = await harness_factory(rounds=[rnd()], final=final)
    ops = await finished(h, db)
    assert (await ops.trials.get(TRIAL_ID)).state == "paused_manual"  # type: ignore[union-attr]

    stored = await ops.resolve(
        TRIAL_ID, tier="B", penalty=4, reviewer="alice", reason="judge outage, read by hand"
    )

    assert (stored.version, stored.tier, stored.decided_by) == (1, "B", "human")
    assert stored.payload["human_override"]["reviewer"] == "alice"
    assert stored.payload["decision"]["rule_id"] == "G-HUMAN-99"
    row = await ops.trials.get(TRIAL_ID)
    assert row is not None and (row.state, row.validity) == ("scored", "valid")
    await assert_projected(h, db)


async def test_resolve_overrides_a_scored_trial_with_a_new_version(
    harness_factory: Any, rnd: Any, db: Database
) -> None:
    h = await harness_factory(rounds=[rnd()])
    ops = await finished(h, db)

    stored = await ops.resolve(
        TRIAL_ID, tier="C", penalty=9, reviewer="arbiter-1", reason="arbitration overturned"
    )

    assert (stored.version, stored.tier) == (2, "C")
    assert stored.payload["human_override"]["previous_tier"] == "P"
    with pytest.raises(OperationRefused):
        await ops.resolve(TRIAL_ID, tier="C", penalty=9, reviewer="x", reason="short")
    await assert_projected(h, db)
