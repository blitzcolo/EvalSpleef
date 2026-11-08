from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from evalspleef.control.cli.app import app

PRESET = {
    "id": "qwen3.8-27b/beam-rtx5090x1/sglang",
    "model": "orcarouter/Qwen3.8-27B-Uncensored-NVFP4",
    "hardware": {"provider": "beam", "gpu": "RTX5090", "gpu_count": 1, "vram_gb_per_gpu": 32},
    "engine": "sglang",
    "engine_args": ["--mem-fraction-static", "0.88"],
    "context_length": 32768,
    "facts": {"weights_gib": 23.02},
}


def _cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.output


def _ids(table: Path, *filters: str) -> list[str]:
    code, out = _cli("presets", "list", "--presets", str(table), "--json", *filters)
    assert code == 0, out
    return [entry["id"] for entry in json.loads(out)]


def test_presets_can_be_added_found_edited_verified_copied_and_removed(tmp_path: Path) -> None:
    table = tmp_path / "presets.yaml"
    entry = tmp_path / "entry.yaml"
    entry.write_text(yaml.safe_dump(PRESET), encoding="utf-8")
    pid = PRESET["id"]

    assert _cli("presets", "add", str(entry), "--presets", str(table))[0] == 0
    assert _cli("presets", "add", str(entry), "--presets", str(table))[0] == 1  # duplicate id
    assert _ids(table, "--gpu", "rtx5090", "--gpus", "1") == [pid]
    assert _ids(table, "--gpus", "2") == []

    two = "qwen3.8-27b/beam-rtx5090x2/sglang"
    at = ("--presets", str(table))
    edits = ("--set", "context_length=65536", "--set", "notes=needs 0.88")
    assert _cli("presets", "edit", pid, *at, *edits, "--unset", "facts.weights_gib")[0] == 0
    verify = ("--result", "ok", "--summary", "ready in 161 s", "--date", "2026-11-08")
    assert _cli("presets", "verify", pid, *at, *verify)[0] == 0
    assert _cli("presets", "copy", pid, two, *at)[0] == 0
    assert _cli("presets", "edit", two, *at, "--set", "hardware.gpu_count=2")[0] == 0

    stored = {preset["id"]: preset for preset in yaml.safe_load(table.read_text())["presets"]}
    assert stored[pid]["context_length"] == 65536
    assert stored[pid]["notes"] == "needs 0.88"
    assert stored[pid]["facts"] == {}
    assert stored[pid]["verified"] == [
        {"date": "2026-11-08", "result": "ok", "summary": "ready in 161 s"}
    ]
    copy = stored[two]
    assert copy["hardware"]["gpu_count"] == 2 and copy["verified"] == []

    code, out = _cli("presets", "search", "needs 0.88", *at, "--json")
    assert code == 0 and sorted(entry["id"] for entry in json.loads(out)) == [pid, two]
    code, out = _cli("presets", "search", "161 s", *at, "--json")
    assert code == 0 and [entry["id"] for entry in json.loads(out)] == [pid]
    code, out = _cli("presets", "show", pid, "--presets", str(table), "--json")
    assert code == 0 and json.loads(out)["digest"].startswith("sha256:")

    assert _cli("presets", "remove", pid, "--presets", str(table))[0] == 0
    assert _ids(table) == [two]
    assert _cli("presets", "remove", pid, "--presets", str(table))[0] == 1


def test_an_invalid_edit_is_refused_and_leaves_the_file_alone(tmp_path: Path) -> None:
    table = tmp_path / "presets.yaml"
    entry = tmp_path / "entry.yaml"
    entry.write_text(yaml.safe_dump(PRESET), encoding="utf-8")
    assert _cli("presets", "add", str(entry), "--presets", str(table))[0] == 0
    before = table.read_text()
    code, out = _cli(
        "presets", "edit", PRESET["id"], "--presets", str(table), "--set", "hardware.gpu_count=9"
    )
    assert code == 2, out
    assert table.read_text() == before
    code, _ = _cli("presets", "edit", PRESET["id"], "--presets", str(table), "--set", "id=other")
    assert code == 2
    assert table.read_text() == before
