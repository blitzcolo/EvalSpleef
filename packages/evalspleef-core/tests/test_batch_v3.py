"""Batch lifecycle transitions and guards (PRD appendix B.1, FR-FLOW-01/02)."""

from __future__ import annotations

import json
from itertools import product

import pytest

from evalspleef.core.batch import (
    TRANSITIONS,
    BatchGuardFailed,
    BatchMachine,
    BatchState,
    FreezeGuards,
    IllegalBatchTransition,
    RunGuards,
)
from evalspleef.core.events import EventEnvelope, EventType

ALL_STATES = tuple(BatchState)


def freeze_guards(**overrides: object) -> FreezeGuards:
    values: dict[str, object] = {
        "lint_passed": True,
        "budget_approved": True,
        "image_digests_locked": True,
        "endpoint_probe_reports_frozen": True,
        "model_revisions_frozen": True,
        "template_digest_map_frozen": True,
        "gateway_version_frozen": True,
    }
    values.update(overrides)
    return FreezeGuards(**values)  # type: ignore[arg-type]


def run_guards(**overrides: object) -> RunGuards:
    values: dict[str, object] = {
        "endpoints_ready_warm": True,
        "carrier_capacity_satisfiable": True,
        "isolation_profiles_valid": True,
    }
    values.update(overrides)
    return RunGuards(**values)  # type: ignore[arg-type]


def test_happy_path_walks_every_state_and_returns_payloads() -> None:
    machine = BatchMachine()
    payloads = [
        machine.move(BatchState.FROZEN, freeze_guards(), reason="planned"),
        machine.move(BatchState.RUNNING, run_guards()),
        machine.move(BatchState.GRADED),
        machine.move(BatchState.AUDITED),
        machine.move(BatchState.PUBLISHED),
        machine.move(BatchState.ARCHIVED),
    ]
    assert machine.state == BatchState.ARCHIVED
    assert [payload["to_state"] for payload in payloads] == [
        "frozen",
        "running",
        "graded",
        "audited",
        "published",
        "archived",
    ]
    assert payloads[0]["reason"] == "planned"
    assert payloads[0]["guards_checked"] == list(freeze_guards().conditions())
    assert payloads[1]["guards_checked"] == list(run_guards().conditions())
    for payload in payloads:
        assert json.loads(json.dumps(payload)) == payload
        # The payload can be dropped into an event envelope unchanged.
        envelope = EventEnvelope(
            event_id="evt_01J00000000000000000000AB",
            ts="2026-11-06T12:00:00Z",
            batch_id="2026-11-b1",
            type=EventType.INCIDENT_OPENED,
            actor="batch-controller",
            payload=payload,
        )
        assert EventEnvelope.from_json_line(envelope.to_json_line()) == envelope


def test_audit_failure_can_return_to_running() -> None:
    machine = BatchMachine(BatchState.AUDITED)
    assert machine.can_move(BatchState.RUNNING)
    payload = machine.move(BatchState.RUNNING, run_guards(), reason="audit found a gap")
    assert payload["from_state"] == "audited"
    assert machine.state == BatchState.RUNNING


def test_transition_table_is_exactly_prd_appendix_b1() -> None:
    assert {
        BatchState.DRAFT: frozenset({BatchState.FROZEN}),
        BatchState.FROZEN: frozenset({BatchState.RUNNING}),
        BatchState.RUNNING: frozenset({BatchState.GRADED}),
        BatchState.GRADED: frozenset({BatchState.AUDITED}),
        BatchState.AUDITED: frozenset({BatchState.PUBLISHED, BatchState.RUNNING}),
        BatchState.PUBLISHED: frozenset({BatchState.ARCHIVED}),
        BatchState.ARCHIVED: frozenset(),
    } == TRANSITIONS


@pytest.mark.parametrize(("source", "target"), list(product(ALL_STATES, ALL_STATES)))
def test_every_unsupported_edge_is_illegal(source: BatchState, target: BatchState) -> None:
    machine = BatchMachine(source)
    if target in TRANSITIONS[source]:
        assert machine.can_move(target)
        return
    assert not machine.can_move(target)
    with pytest.raises(IllegalBatchTransition, match=f"{source} -> {target}"):
        machine.move(target)
    assert machine.state == source


def test_freeze_guard_failure_names_condition_and_reason() -> None:
    machine = BatchMachine()
    guards = freeze_guards(lint_passed=False, reasons={"lint_passed": "3 lint errors"})
    with pytest.raises(BatchGuardFailed) as raised:
        machine.move(BatchState.FROZEN, guards)
    assert raised.value.failed_conditions == ("lint_passed",)
    assert raised.value.failures[0].reason == "3 lint errors"
    assert "lint_passed" in str(raised.value)
    assert machine.state == BatchState.DRAFT


def test_missing_freeze_guards_fail_every_condition() -> None:
    machine = BatchMachine()
    with pytest.raises(BatchGuardFailed) as raised:
        machine.move(BatchState.FROZEN)
    assert raised.value.failed_conditions == tuple(FreezeGuards().conditions())
    assert machine.state == BatchState.DRAFT


def test_freeze_guards_cover_prd_flow_01_items() -> None:
    conditions = FreezeGuards().conditions()
    assert set(conditions) == {
        "lint_passed",
        "budget_approved",
        "image_digests_locked",
        "endpoint_probe_reports_frozen",
        "model_revisions_frozen",
        "template_digest_map_frozen",
        "gateway_version_frozen",
    }


def test_run_guard_failure_names_all_failed_conditions() -> None:
    machine = BatchMachine(BatchState.FROZEN)
    guards = run_guards(
        endpoints_ready_warm=False,
        isolation_profiles_valid=False,
        reasons={"isolation_profiles_valid": "profile digest mismatch"},
    )
    with pytest.raises(BatchGuardFailed) as raised:
        machine.move(BatchState.RUNNING, run_guards=guards)
    assert raised.value.failed_conditions == (
        "endpoints_ready_warm",
        "isolation_profiles_valid",
    )
    assert "profile digest mismatch" in str(raised.value)
    assert machine.state == BatchState.FROZEN


def test_missing_run_guards_fail_every_condition() -> None:
    machine = BatchMachine(BatchState.FROZEN)
    with pytest.raises(BatchGuardFailed) as raised:
        machine.move(BatchState.RUNNING)
    assert raised.value.failed_conditions == tuple(RunGuards().conditions())
    assert machine.state == BatchState.FROZEN


def test_unrelated_guards_are_not_required() -> None:
    machine = BatchMachine(BatchState.RUNNING)
    payload = machine.move(BatchState.GRADED)
    assert payload["guards_checked"] == []
