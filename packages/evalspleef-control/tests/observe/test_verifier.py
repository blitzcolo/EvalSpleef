"""Hard-gate loading, hash-lock manifests, host-tool marking and live gate execution."""

from __future__ import annotations

import pytest

from evalspleef.control.observe.verifier import (
    VerificationError,
    _gate_command,
    _json_blobs,
    check_hash_locks,
    find_hash_manifest,
    load_hard_gates,
    parse_hash_manifest,
    requires_host_tools,
    run_verification,
)
from evalspleef.control.taskpack import TaskPack
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider

ALL_PACKS = (
    "logic-001",
    "logic-002",
    "data-001",
    "debug-001",
    "backend-001",
    "web-001",
    "video-001",
    "macos-001",
    "vulkan-001",
    "harmony-001",
)


@pytest.mark.parametrize("task_id", ALL_PACKS)
def test_hard_gates_parse_for_every_pack(pack_factory, task_id: str) -> None:
    pack: TaskPack = pack_factory(task_id)
    gates = load_hard_gates(pack)
    assert gates, f"{task_id} declares no parseable hard gates"
    ids = [gate.gate_id for gate in gates]
    assert len(ids) == len(set(ids))
    for gate in gates:
        assert gate.script
        assert gate.timeout_s > 0


def test_verify_submission_form(pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    gates = load_hard_gates(pack)
    assert [g.gate_id for g in gates] == ["HG1", "HG2", "HG3"]
    assert all(g.script == "hidden-tests/verify_submission.py" for g in gates)
    assert gates[0].pass_condition == "HG1_answer_correct == true"


def test_run_sh_and_command_forms(pack_factory) -> None:
    pack: TaskPack = pack_factory("debug-001")
    gates = load_hard_gates(pack)
    assert gates[0].script == "hidden-tests/run.sh"
    assert gates[1].script == "hidden-tests/check_locks.py"
    assert gates[2].script.startswith("command:")


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("logic-001", None),
        ("logic-002", None),
        ("data-001", None),
        ("debug-001", None),
        ("backend-001", None),
        ("web-001", None),
        ("macos-001", "toolchain"),
        ("harmony-001", "toolchain"),
        ("vulkan-001", "GPU"),
        ("video-001", "ffmpeg"),
    ],
)
def test_requires_host_tools(pack_factory, task_id: str, expected: str | None) -> None:
    pack: TaskPack = pack_factory(task_id)
    reason = requires_host_tools(pack)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_hash_manifest_discovery_and_parsing(pack_factory) -> None:
    logic: TaskPack = pack_factory("logic-001")
    manifest = find_hash_manifest(logic)
    assert manifest is not None and manifest.name == "hashes.json"
    entries = parse_hash_manifest(manifest)
    locked = {entry.relative: entry.sha256 for entry in entries}
    assert locked["../workspace/instances.json"] == (
        "d8c488889f89cf7a85f09fa967e6f11e00c648975a1df60d8f7c015a0b1578bd"
    )
    assert "run.sh" in locked  # verifier-side file, skipped during lock checks

    debug: TaskPack = pack_factory("debug-001")
    manifest = find_hash_manifest(debug)
    assert manifest is not None and manifest.name == "locks.sha256"
    entries = parse_hash_manifest(manifest)
    assert [(e.relative, len(e.sha256)) for e in entries] == [("tests/test_public.py", 64)]

    harmony: TaskPack = pack_factory("harmony-001")
    manifest = find_hash_manifest(harmony)
    assert manifest is not None and manifest.name == "locks.json"
    assert len(parse_hash_manifest(manifest)) == 2


def test_check_hash_locks_flags_tampering(tmp_path, pack_factory) -> None:
    pack: TaskPack = pack_factory("debug-001")
    manifest = find_hash_manifest(pack)
    assert manifest is not None
    tree = tmp_path / "tree"
    good = tree / "tests" / "test_public.py"
    good.parent.mkdir(parents=True)
    good.write_bytes((pack.root / "starter/tests/test_public.py").read_bytes())
    assert check_hash_locks(manifest, tree, protected=("tests",)) == ()

    good.write_text("# tampered\n")
    mismatches = check_hash_locks(manifest, tree, protected=("tests",))
    assert len(mismatches) == 1
    assert mismatches[0].relative == "tests/test_public.py"
    assert mismatches[0].actual is not None

    good.unlink()
    missing = check_hash_locks(manifest, tree, protected=("tests",))
    assert len(missing) == 1 and missing[0].actual is None


def test_check_hash_locks_resolves_workspace_relative_keys(tmp_path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    manifest = find_hash_manifest(pack)
    assert manifest is not None
    tree = tmp_path / "tree"
    inst = tree / "instances.json"
    inst.parent.mkdir(parents=True)
    inst.write_bytes((pack.root / "workspace/instances.json").read_bytes())
    assert check_hash_locks(manifest, tree, protected=("instances.json",)) == ()
    inst.write_text("{}")
    (mismatch,) = check_hash_locks(manifest, tree, protected=("instances.json",))
    assert mismatch.relative == "../workspace/instances.json"


async def _stage_verifier(
    provider: MockSandboxProvider, pack: TaskPack, workspace, hmod
) -> MockSandbox:
    trial = hmod.planned_trial(pack)
    sandbox = await provider.create(hmod.make_spec(trial, "verifier", "lease-verifier-1"))
    assert isinstance(sandbox, MockSandbox)
    await sandbox.upload(pack.root / "hidden-tests", "/verify/hidden-tests")
    await sandbox.upload(workspace, "/verify/workspace")
    return sandbox


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("logic-001.reference", {"HG1": True, "HG2": True, "HG3": True}),
        ("logic-001.empty", {"HG1": False, "HG2": False, "HG3": True}),
        ("logic-001.wrong-answer", {"HG1": False, "HG2": True, "HG3": True}),
        ("logic-001.tampered-input", {"HG1": True, "HG2": True, "HG3": False}),
    ],
)
async def test_gates_run_live_in_mock_sandbox(
    provider, pack_factory, tmp_path, hmod, fixture_name: str, expected: dict[str, bool]
) -> None:
    """The real verify_submission.py runs inside a real subprocess sandbox."""
    pack: TaskPack = pack_factory("logic-001")
    workspace = hmod.fixture_workspace(pack, tmp_path, fixture_name)
    sandbox = await _stage_verifier(provider, pack, workspace, hmod)
    output = await run_verification(sandbox, pack)
    assert {run.gate.gate_id: run.passed for run in output.gates} == expected
    assert not any(run.timed_out for run in output.gates)


async def test_stale_reward_files_deleted_and_reported(
    provider, pack_factory, tmp_path, hmod
) -> None:
    pack: TaskPack = pack_factory("logic-001")
    workspace = hmod.fixture_workspace(pack, tmp_path, "logic-001.reference")
    (workspace / "reward.json").write_text('{"tier": "P"}')  # planted by the candidate
    sandbox = await _stage_verifier(provider, pack, workspace, hmod)
    output = await run_verification(sandbox, pack)
    assert output.planted_rewards == ("workspace/reward.json",)
    find = await sandbox.exec(
        "find workspace -name reward.json -o -name reward.txt",
        cwd="/verify",
        timeout_s=30.0,
    )
    assert find.stdout.decode().strip() == ""


async def test_broken_verifier_reports_failed_gates(provider, pack_factory, tmp_path, hmod) -> None:
    """Missing verifier inputs break the script: gates fail honestly, no infra error."""
    pack: TaskPack = pack_factory("logic-001")
    workspace = hmod.fixture_workspace(pack, tmp_path, "logic-001.reference")
    sandbox = await _stage_verifier(provider, pack, workspace, hmod)
    await sandbox.exec("rm workspace/instances.json", cwd="/verify", timeout_s=30.0)
    output = await run_verification(sandbox, pack)
    assert all(not run.passed for run in output.gates)


def test_escape_script_rejected(pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    gate = load_hard_gates(pack)[0]
    bad = type(gate)(gate.gate_id, gate.kind, "../secrets", gate.required, gate.pass_condition)
    with pytest.raises(VerificationError):
        _gate_command(bad, pack, verify_root="/verify", candidate_dir="workspace")


def test_json_blobs_tolerate_noise() -> None:
    text = 'unittest noise\n{"HG1_x": true, "nested": {"ok": false}}\ntrailing {broken\n'
    blobs = _json_blobs(text)
    assert blobs[0]["HG1_x"] is True  # the outer document parses first
    assert all(isinstance(blob, dict) for blob in blobs)


def test_gate_command_forms(pack_factory) -> None:
    pack: TaskPack = pack_factory("debug-001")
    gates = load_hard_gates(pack)
    cmd, cwd = _gate_command(gates[2], pack, verify_root="/verify", candidate_dir="workspace")
    assert cmd == "python3 -m unittest discover -s tests"
    assert cwd == "/verify/workspace"

    logic: TaskPack = pack_factory("logic-001")
    cmd, cwd = _gate_command(
        load_hard_gates(logic)[0],
        logic,
        verify_root="/verify",
        candidate_dir="workspace",
    )
    assert cmd[:2] == ["python3", "hidden-tests/verify_submission.py"]
    assert "--answer" in cmd and "workspace/answer.json" in cmd
    assert cwd == "/verify"

    data: TaskPack = pack_factory("data-001")
    cmd, _ = _gate_command(
        load_hard_gates(data)[0],
        data,
        verify_root="/verify",
        candidate_dir="workspace",
    )
    assert "--submission" in cmd and "workspace/clean_logs.py" in cmd
