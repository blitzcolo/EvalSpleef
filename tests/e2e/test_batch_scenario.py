"""The end-to-end batch scenario on real local Docker through the real spleef CLI.

One batch, 3 tasks x 2 fake models x 2 epochs, shell terminus, sandbox provider
local-docker, JSONL event store. The fake reference agent applies each pack's
reference solution A through a bash heredoc; the fake empty agent finishes
immediately without tool calls.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evalspleef.core.models import parse_trial_id
from evalspleef.core.state import IllegalTransition, TrialMachine

REPORTS = Path(__file__).resolve().parents[2] / ".omo" / "reports" / "e2e"
BATCH_ID = "2026-11-b1"
TASK_IDS = ("logic-001", "data-001", "debug-001")
REF_MODEL = "fake-ref-agent"
EMPTY_MODEL = "fake-empty-agent"
PASS_TIERS = {"P", "A"}
B_FAMILY = {"B+", "B", "C+", "C"}
EMPTY_RULE_IDS = {"G-BUDGET-F", "G-ABORT-04"}
EPOCHS = 2  # docs/03 9.9: a cell needs >= 2 valid epochs to be ranked
TRIALS = len(TASK_IDS) * 2 * EPOCHS


def _grade_rows(store: Path) -> dict[str, dict[str, Any]]:
    """trial_id -> final grade row from the result store."""
    connection = sqlite3.connect(store)
    try:
        rows = connection.execute(
            "SELECT trial_id, tier, rule_id, payload FROM grade ORDER BY trial_id, version"
        ).fetchall()
    finally:
        connection.close()
    grades: dict[str, dict[str, Any]] = {}
    for trial_id, tier, rule_id, payload in rows:
        record = json.loads(payload)
        # the grade record's decision.rule_id is authoritative (grade schema); the row's
        # rule_id column mirrors it
        decided = (record.get("decision") or {}).get("rule_id")
        grades[trial_id] = {"tier": tier, "rule_id": rule_id or decided, "payload": record}
    return grades


def _event_log(events_dir: Path) -> list[dict[str, Any]]:
    path = events_dir / f"{BATCH_ID}.jsonl"
    assert path.is_file(), f"event log not found: {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _replay_legal(events: list[dict[str, Any]]) -> None:
    """Every transition payload must replay through the core trial state machine."""
    by_trial: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        trial_id = event.get("trial_id")
        if isinstance(trial_id, str):
            by_trial.setdefault(trial_id, []).append(event)
    assert by_trial, "no trial events recorded"
    for trial_id, trial_events in sorted(by_trial.items()):
        # The lease manager records pending -> leased, so the replay starts at pending.
        machine = TrialMachine(trial_id)
        transitions = 0
        for event in trial_events:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            transition = payload.get("transition")
            if not isinstance(transition, dict):
                continue
            transitions += 1
            try:
                machine.apply(str(transition["event"]))
            except IllegalTransition as exc:
                pytest.fail(f"{trial_id}: illegal transition {transition}: {exc}")
        assert transitions > 0, f"{trial_id}: no state transitions recorded"


def _runner_tokens(events: list[dict[str, Any]], trial_id: str) -> tuple[int, int]:
    """Token totals the runner reported in agent.step events (its transcript data)."""
    input_tokens = output_tokens = 0
    for event in events:
        if event.get("trial_id") != trial_id or event.get("type") != "agent.step":
            continue
        payload = event.get("payload") or {}
        input_tokens += int(payload.get("input_tokens") or 0)
        output_tokens += int(payload.get("output_tokens") or 0)
    return input_tokens, output_tokens


@pytest.mark.e2e
def test_full_batch_on_local_docker(e2e: Any) -> None:
    harness = e2e

    # -- 1. configuration ---------------------------------------------------------
    result = harness.run_cli("config-validate", "config", "validate", *harness.options())
    assert result.returncode == 0, result.stderr[-2000:]

    # -- 2. register the three fake models ----------------------------------------
    result = harness.run_cli(
        "models-register", "models", "register", *harness.options(with_batch=False)
    )
    assert result.returncode == 0, result.stderr[-2000:]
    for slug in (REF_MODEL, EMPTY_MODEL, "fake-judge"):
        assert slug in result.stdout

    # -- 3. probe every model through the gateway ---------------------------------
    result = harness.run_cli("models-probe", "models", "probe", *harness.options(with_batch=False))
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.count("probes") == 3

    # -- 4. plan, freeze -----------------------------------------------------------
    result = harness.run_cli("batch-plan", "batch", "plan", *harness.options())
    assert result.returncode == 0, result.stderr[-2000:]
    for task in TASK_IDS:
        assert f"{task}.{REF_MODEL}.terminus" in result.stdout
        assert f"{task}.{EMPTY_MODEL}.terminus" in result.stdout

    result = harness.run_cli("batch-freeze", "batch", "freeze", *harness.options())
    assert result.returncode == 0, result.stderr[-2000:]
    assert "version matrix hash" in result.stdout

    # -- 5. run the batch -----------------------------------------------------------
    result = harness.run_cli("batch-run", "batch", "run", *harness.options())
    assert result.returncode == 0, result.stderr[-2000:]
    assert "批次报告" in result.stdout
    # batch run exits non-zero on any trial row the event log does not derive.
    assert f"event projection: {TRIALS} trial row(s) checked" in result.stdout
    (REPORTS / "batch-report.md").write_text(result.stdout, encoding="utf-8")

    # -- 6. report + grade assertions ------------------------------------------------
    release = harness.store_path.parent / "release"
    result = harness.run_cli(
        "batch-report", "batch", "report", "--json", "--release", str(release), *harness.options()
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["batch_id"] == BATCH_ID
    assert len(report["cells"]) == 6, f"expected 6 cells, got {len(report['cells'])}"
    cells = {cell["cell_id"]: cell for cell in report["cells"]}

    grades = _grade_rows(harness.store_path)
    assert len(grades) == TRIALS, f"expected {TRIALS} graded trials, got {sorted(grades)}"
    # trial.versions (PRD FR-MOD-09) is on every row, recorded from the real sandbox.
    connection = sqlite3.connect(harness.store_path)
    try:
        row_versions = [
            json.loads(extensions).get("versions")
            for (extensions,) in connection.execute("SELECT extensions FROM trial")
        ]
    finally:
        connection.close()
    assert len(row_versions) == TRIALS
    for versions in row_versions:
        assert versions is not None
        assert versions["sandbox_adapter"] == "docker"
        assert versions["deployment_adapter"] == "openai-compatible"
        assert versions["freeze_hash"].startswith("sha256:")

    # The release package (FR-RPT-2/4) drills every cell down to its sources.
    drilldown = json.loads((release / "drilldown.json").read_text(encoding="utf-8"))
    assert len(drilldown) == 6
    for entry in drilldown:
        assert entry["deployment_adapter"] == "openai-compatible"
        assert entry["sandbox_adapter"] == "docker"
        assert entry["declared_isolation"] == "L1"
        assert entry["cost_basis"] == "assumed"
        assert len(entry["trials"]) == EPOCHS
    probes = json.loads((release / "probes.json").read_text(encoding="utf-8"))
    assert len(probes) == 2 and all(p["total"] > 0 for p in probes.values())
    manifest = json.loads((release / "versions.json").read_text(encoding="utf-8"))
    assert len(manifest["trials"]) == TRIALS and manifest["freeze"]["batch_id"] == BATCH_ID
    assert "## local-docker (docker)" in (release / "isolation.md").read_text(encoding="utf-8")
    costs = json.loads((release / "costs.json").read_text(encoding="utf-8"))
    assert {kind["kind"] for kind in costs["kinds"]} == {"tokens", "gpu", "sandbox", "infra"}
    assert (release / "leaderboard.md").is_file() and (release / "judges.json").is_file()
    trials_of_cell: dict[str, list[str]] = {}
    for trial_id in grades:
        parts = parse_trial_id(trial_id)
        trials_of_cell.setdefault(f"{parts.task_id}.{parts.model}.{parts.shell}", []).append(
            trial_id
        )
    assert set(trials_of_cell) == set(cells), f"graded cells {sorted(trials_of_cell)}"

    for cell_id, trial_ids in sorted(trials_of_cell.items()):
        cell = cells[cell_id]
        model = parse_trial_id(trial_ids[0]).model
        for trial_id in trial_ids:
            grade = grades[trial_id]
            print(
                f"{cell_id} {trial_id}: tier={grade['tier']} rule={grade['rule_id']} "
                f"cell={cell['status']}/{cell['tier']}"
            )
            if model == REF_MODEL:
                if grade["tier"] in B_FAMILY:
                    feedback_rounds = grade["payload"].get("feedback_rounds") or []
                    assert feedback_rounds, (
                        f"{trial_id}: B-family tier without a recorded feedback round"
                    )
                assert grade["tier"] in PASS_TIERS | B_FAMILY, (
                    f"{trial_id}: reference agent must pass, got tier {grade['tier']} "
                    f"rule {grade['rule_id']}"
                )
            else:
                assert grade["tier"] == "F", f"{trial_id}: empty agent must fail, got {grade}"
                assert grade["rule_id"] in EMPTY_RULE_IDS, (
                    f"{trial_id}: unexpected rule {grade['rule_id']}"
                )
        assert len(trial_ids) == EPOCHS, f"{cell_id}: expected {EPOCHS} epochs, got {trial_ids}"
        assert cell["status"] == "scored", f"cell not scored: {cell}"
        if model == EMPTY_MODEL:
            assert cell["tier"] == "F", f"{cell_id}: empty agent cell must be F, got {cell}"
        else:
            assert cell["tier"] in PASS_TIERS | B_FAMILY, f"{cell_id}: cell must pass: {cell}"

    # -- 7. canary scan clean for every trial ----------------------------------------
    events = _event_log(harness.events_dir)
    scans = {
        event["trial_id"]: event
        for event in events
        if event.get("type") == "canary.scanned" and event.get("trial_id")
    }
    assert len(scans) == TRIALS, f"expected {TRIALS} canary scans, got {sorted(scans)}"
    providers = {
        (event.get("payload") or {}).get("provider")
        for event in events
        if event.get("type") == "sandbox.provisioned"
    }
    assert providers == {"local-docker"}, f"leases ran outside real Docker: {providers}"
    for trial_id, event in scans.items():
        payload = event.get("payload") or {}
        assert payload.get("passed") is True, f"canary scan failed for {trial_id}: {payload}"

    # -- 8. legal event order for every trial -----------------------------------------
    _replay_legal(events)

    # -- 9. gateway usage matches the runner-reported token counts ---------------------
    usage_by_trial: dict[str, list[dict[str, Any]]] = {}
    for trial_id in grades:
        records = harness.gateway.admin_json(f"/admin/usage?trial_id={trial_id}")
        assert isinstance(records, list) and records, f"no gateway usage for {trial_id}"
        usage_by_trial[trial_id] = records
    (REPORTS / "gateway" / "usage.json").write_text(
        json.dumps(usage_by_trial, indent=2, sort_keys=True), encoding="utf-8"
    )
    for trial_id, records in usage_by_trial.items():
        gateway_input = sum(int(record["input_tokens"]) for record in records)
        gateway_output = sum(int(record["output_tokens"]) for record in records)
        runner_input, runner_output = _runner_tokens(events, trial_id)
        print(
            f"usage {trial_id}: gateway {gateway_input}+{gateway_output} tokens, "
            f"runner {runner_input}+{runner_output}"
        )
        assert (gateway_input, gateway_output) == (runner_input, runner_output), (
            f"{trial_id}: gateway usage does not match the runner transcript tokens"
        )

    # -- 10. no spleef container survives ------------------------------------------------
    remaining = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=spleef"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert remaining.returncode == 0
    body = [line for line in remaining.stdout.splitlines() if line.strip()]
    assert len(body) <= 1 and (not body or body[0].startswith("CONTAINER ID")), (
        f"containers with label 'spleef' remain:\n{remaining.stdout}"
    )
    remaining = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", "label=spleef.provider"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert remaining.returncode == 0
    assert remaining.stdout.strip() == "", (
        f"containers with label 'spleef.provider' remain:\n{remaining.stdout}"
    )
