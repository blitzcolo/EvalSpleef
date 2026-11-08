"""Hash-locked inputs are never canary targets (I12 canary-vs-hash-lock fix).

A canary write changes a file's bytes, which the pack's hidden tests detect as a
broken hash lock or a failed integrity gate. ``stage_workspace`` must therefore
skip hash-locked and protected paths as placements and move the token to the
pack's other declared placements (created hidden dotfiles like ``.env.example``);
when nothing legal remains, S3 still fails.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evalspleef.control.taskpack import load_task_pack
from evalspleef.control.trial.errors import CanaryInjectionError
from evalspleef.control.trial.prepare import lock_protected_paths, stage_workspace
from evalspleef.control.trial.runner import _workspace_paths

REPO = Path(__file__).resolve().parents[4]
TOKEN = "CNRY-" + "a" * 8 + "-" + "b" * 8


def _copy_pack(task_id: str, tmp_path: Path) -> Path:
    root = tmp_path / "pack" / task_id
    shutil.copytree(REPO / "tasks" / task_id, root)
    return root


def _stage(pack_root: Path, tmp_path: Path) -> object:
    pack = load_task_pack(pack_root)
    return stage_workspace(
        pack=pack,
        workspace=_workspace_paths(pack),
        trial_id="t1",
        token=TOKEN,
        scratch=tmp_path / "scratch",
    )


def test_lock_protected_paths_derives_from_manifest_and_task_yaml(tmp_path: Path) -> None:
    pack = load_task_pack(_copy_pack("logic-001", tmp_path))
    protected = lock_protected_paths(pack)
    assert "instances.json" in protected  # locked by hidden-tests/hashes.json

    pack = load_task_pack(_copy_pack("data-001", tmp_path))
    protected = lock_protected_paths(pack)
    assert {"access.log", "expected_sample.json"} <= protected


def test_hash_locked_file_is_never_a_canary_target(tmp_path: Path) -> None:
    root = _copy_pack("logic-001", tmp_path)
    staged = _stage(root, tmp_path)
    placed = {placement.path for placement in staged.placements}
    assert "instances.json" not in placed
    assert ".env.example" in placed  # the pack's other declared placement, created
    originals = (root / "workspace" / "instances.json").read_bytes()
    staged_input = (staged.directory / "instances.json").read_bytes()
    assert staged_input == originals  # byte-identical: the lock still holds
    assert staged.hashes.keys() == placed
    assert TOKEN.encode() in (staged.directory / ".env.example").read_bytes()


def test_data_pack_canary_moves_off_the_locked_log(tmp_path: Path) -> None:
    root = _copy_pack("data-001", tmp_path)
    staged = _stage(root, tmp_path)
    placed = {placement.path for placement in staged.placements}
    assert "access.log" not in placed
    assert "expected_sample.json" not in placed
    assert ".env.example" in placed
    assert (staged.directory / "access.log").read_bytes() == (
        root / "workspace" / "access.log"
    ).read_bytes()


def test_s3_still_fails_when_only_locked_files_could_carry_the_token(tmp_path: Path) -> None:
    root = _copy_pack("logic-001", tmp_path)
    (root / "canary.md").write_text(
        "# canary\n\n## 1. 埋点位置\n\n| 编号 | 位置 |\n| --- | --- |\n"
        "| C1 | 工作区 `instances.json` |\n",
        encoding="utf-8",
    )
    with pytest.raises(CanaryInjectionError):
        _stage(root, tmp_path)
