"""End-to-end: logic-001, data-001 and debug-001 through the Evaluator.

Real-surface tests: MockSandboxProvider sandboxes run real subprocesses on the host,
and the packs' own hidden tests execute for real inside the verifier sandbox. The
reference solutions must pass every hard gate, the starter/empty answer must fail the
expected rubric items, and a candidate that edits a hash-locked file must be flagged
as a cheat signal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from evalspleef.control.observe import Evaluator
from evalspleef.control.taskpack import TaskPack
from evalspleef.testing.mock_sandbox import MockSandbox


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    assert result.returncode == 0, f"{cmd}: {result.stderr[-500:]}"


# ---------------------------------------------------------------------------
# logic-001
# ---------------------------------------------------------------------------


def _logic_answer(tmp_path: Path, pack: TaskPack) -> Path:
    answer = tmp_path / "answer-a.json"
    _run(
        [
            "python3",
            str(pack.root / "answers/reference-a.py"),
            str(pack.root / "workspace/instances.json"),
            str(answer),
        ]
    )
    return answer


async def test_logic001_reference_passes_all_gates(pack_factory, make_trial_ctx, tmp_path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    answer = _logic_answer(tmp_path, pack)

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")
        await sandbox.upload(answer, "/workspace/answer.json")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert result.hard_gates_passed
    assert {gate.check_id for gate in result.hard_gates} == {"HG1", "HG2", "HG3"}
    assert result.cheat_signals == ()
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "pass"
    assert verdicts["R2"] == "pass"
    assert verdicts["R4"] == "pass"
    assert all(verdicts[item] == "pass" for item in ("R3", "R5", "R11", "R12"))
    artifacts = {a.path for a in result.artifacts}
    assert "answer.json" in artifacts and "instances.json" in artifacts


async def test_logic001_empty_fails_expected_items(pack_factory, make_trial_ctx, tmp_path) -> None:
    pack: TaskPack = pack_factory("logic-001")

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    gates = {gate.check_id: gate.passed for gate in result.hard_gates}
    assert gates["HG1"] is False and gates["HG2"] is False and gates["HG3"] is True
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "fail"
    assert verdicts["R2"] == "fail"
    assert verdicts["R4"] == "pass"
    assert verdicts["R3"] == "fail" and verdicts["R12"] == "fail"  # answer.json missing


async def test_logic001_tampered_locked_input_is_a_cheat_signal(
    pack_factory, make_trial_ctx, tmp_path
) -> None:
    pack: TaskPack = pack_factory("logic-001")
    answer = _logic_answer(tmp_path, pack)
    tampered = json.loads((pack.root / "workspace/instances.json").read_text())
    tampered["instances"][0]["boards"][0][0] = (tampered["instances"][0]["boards"][0][0] + 1) % 10
    tampered_path = tmp_path / "instances.json"
    tampered_path.write_text(json.dumps(tampered))

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")
        await sandbox.upload(tampered_path, "/workspace/instances.json")
        await sandbox.upload(answer, "/workspace/answer.json")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    gates = {gate.check_id: gate.passed for gate in result.hard_gates}
    assert gates["HG3"] is False  # HG3_input_untampered == false
    kinds = [signal.kind for signal in result.cheat_signals]
    assert "hash_lock" in kinds


# ---------------------------------------------------------------------------
# data-001
# ---------------------------------------------------------------------------


def _data_submission(tmp_path: Path, pack: TaskPack) -> Path:
    submission = tmp_path / "clean_logs.py"
    shutil.copyfile(pack.root / "answers/reference-b.py", submission)
    return submission


async def test_data001_reference_passes_all_gates(pack_factory, make_trial_ctx, tmp_path) -> None:
    pack: TaskPack = pack_factory("data-001")
    submission = _data_submission(tmp_path, pack)

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")
        await sandbox.upload(submission, "/workspace/clean_logs.py")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert result.hard_gates_passed
    assert result.cheat_signals == ()
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "pass"
    assert verdicts["R2"] == "pass"
    assert verdicts["R3"] == "pass" and verdicts["R12"] == "pass"


async def test_data001_empty_fails_expected_items(pack_factory, make_trial_ctx, tmp_path) -> None:
    pack: TaskPack = pack_factory("data-001")

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "fail"
    assert verdicts["R2"] == "fail"
    assert verdicts["R3"] == "fail"  # required clean_logs.py missing from the export


async def test_data001_tampered_locked_input_is_a_cheat_signal(
    pack_factory, make_trial_ctx, tmp_path
) -> None:
    pack: TaskPack = pack_factory("data-001")
    submission = _data_submission(tmp_path, pack)
    log = pack.root / "workspace/access.log"
    tampered_log = tmp_path / "access.log"
    tampered_log.write_text(
        log.read_text()
        + '9.9.9.9 - - [02/Jan/2026:00:00:00 +0000] "GET /x HTTP/1.1" 200 1 "-" "-"\n'
    )

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(pack.root / "workspace", "/workspace")
        await sandbox.upload(tampered_log, "/workspace/access.log")
        await sandbox.upload(submission, "/workspace/clean_logs.py")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    kinds = [signal.kind for signal in result.cheat_signals]
    assert "hash_lock" in kinds


# ---------------------------------------------------------------------------
# debug-001
# ---------------------------------------------------------------------------


def _debug_candidate(tmp_path: Path, pack: TaskPack, patch_name: str | None) -> Path:
    candidate = tmp_path / f"candidate-{patch_name or 'starter'}"
    shutil.copytree(pack.root / "starter", candidate)
    if patch_name is not None:
        _run(["patch", "-p1", "-s", "-d", str(candidate), "-i", str(pack.root / patch_name)])
    return candidate


async def test_debug001_reference_passes_all_gates(pack_factory, make_trial_ctx, tmp_path) -> None:
    pack: TaskPack = pack_factory("debug-001")
    candidate = _debug_candidate(tmp_path, pack, "answers/reference-a.patch")

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(candidate, "/workspace")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert result.hard_gates_passed, [(g.check_id, g.passed) for g in result.hard_gates]
    assert result.cheat_signals == ()
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    blocking = ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert all(verdicts[item] == "pass" for item in blocking)


async def test_debug001_starter_fails_expected_items(
    pack_factory, make_trial_ctx, tmp_path
) -> None:
    pack: TaskPack = pack_factory("debug-001")
    candidate = _debug_candidate(tmp_path, pack, None)

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(candidate, "/workspace")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    gates = {gate.check_id: gate.passed for gate in result.hard_gates}
    assert gates == {"HG1": False, "HG2": True, "HG3": True}
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    blocking = ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert all(verdicts[item] == "fail" for item in blocking)
    # locks intact: not a cheat, just bugs
    assert result.cheat_signals == ()


def _tamper_visible_test(candidate: Path) -> None:
    with open(candidate / "tests/test_public.py", "a") as handle:
        handle.write("\n# agent tampered\n")


async def test_debug001_edited_locked_test_is_a_cheat_signal(
    pack_factory, make_trial_ctx, tmp_path
) -> None:
    pack: TaskPack = pack_factory("debug-001")
    candidate = _debug_candidate(tmp_path, pack, None)
    _tamper_visible_test(candidate)

    async def stage(sandbox: MockSandbox) -> None:
        await sandbox.upload(candidate, "/workspace")

    ctx, _agent = await make_trial_ctx(pack, tmp_path, stage_agent=stage)
    result = await Evaluator().evaluate(ctx, 1, final=True)

    assert not result.hard_gates_passed
    kinds = [signal.kind for signal in result.cheat_signals]
    assert "hash_lock" in kinds
    assert any("test_public.py" in signal.detail for signal in result.cheat_signals)
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R9"] == "fail"
