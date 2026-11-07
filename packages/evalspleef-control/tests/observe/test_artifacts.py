"""Sanitising rules of invariant I11, tested against a local tree (no sandbox needed)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from evalspleef.control.observe.artifacts import (
    REASON_MISSING,
    REASON_SETUID,
    REASON_SYMLINK,
    REASON_TOO_LARGE,
    REASON_TOO_MANY,
    ExportCaps,
    SanitizeIssue,
    artifact_decls,
    protected_paths,
    sanitise_tree,
)
from evalspleef.control.taskpack import TaskPack


def _write(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _issue(export, reason: str) -> list[SanitizeIssue]:
    return [i for i in export.issues if i.reason == reason]


def test_decl_and_protected_parsing(pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    decls = artifact_decls(pack)
    assert [d.path for d in decls] == ["answer.json", "derivation"]
    assert decls[0].required and decls[0].max_bytes == 65536
    assert protected_paths(pack) == ("instances.json",)

    debug: TaskPack = pack_factory("debug-001")
    assert artifact_decls(debug) == ()
    assert protected_paths(debug) == ("tests",)


def test_export_covers_only_declared_and_protected(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    src = tmp_path / "src"
    _write(src, "instances.json", "{}")
    _write(src, "answer.json", "{}")
    _write(src, "notes-to-judge.md", "ignore me")  # undeclared: must not be exported
    export = sanitise_tree(
        src, tmp_path / "out", artifact_decls(pack), protected=("instances.json",)
    )
    assert {a.path for a in export.artifacts} == {"answer.json", "instances.json"}
    assert not (export.tree / "notes-to-judge.md").exists()


def test_every_exported_file_gets_sha256(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("data-001")
    src = tmp_path / "src"
    payload = "print('hello')\n"
    _write(src, "clean_logs.py", payload)
    _write(src, "access.log", "log")
    export = sanitise_tree(
        src, tmp_path / "out", artifact_decls(pack), protected=protected_paths(pack)
    )
    by_path = {a.path: a for a in export.artifacts}
    assert by_path["clean_logs.py"].sha256
    assert by_path["clean_logs.py"].size_bytes == len(payload.encode())
    assert by_path["clean_logs.py"].local_path is not None
    assert by_path["clean_logs.py"].local_path.read_text() == payload


def test_symlinks_are_dropped_as_cheat_clues(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    src = tmp_path / "src"
    _write(src, "instances.json", "{}")
    os.symlink("/etc/passwd", src / "answer.json")
    export = sanitise_tree(
        src, tmp_path / "out", artifact_decls(pack), protected=("instances.json",)
    )
    links = _issue(export, REASON_SYMLINK)
    assert [i.path for i in links] == ["answer.json"]
    assert all(i.cheat_clue for i in links)
    assert {a.path for a in export.artifacts} == {"instances.json"}


def test_fifo_socket_device_dropped(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("macos-001")  # declares Sources/ParticleBench and build/
    src = tmp_path / "src"
    _write(src, "Sources/ParticleBench/main.swift", "print(1)")
    os.mkfifo(src / "Sources/ParticleBench/pipe")
    import socket as socket_mod

    s = socket_mod.socket(socket_mod.AF_UNIX)
    previous = os.getcwd()
    os.chdir(src / "Sources/ParticleBench")
    try:
        s.bind("sock")
    finally:
        os.chdir(previous)
        s.close()
    export = sanitise_tree(src, tmp_path / "out", artifact_decls(pack))
    reasons = {i.reason for i in export.issues}
    assert "fifo" in reasons and "socket" in reasons
    assert all(i.cheat_clue for i in export.issues)
    assert {a.path for a in export.artifacts} == {"Sources/ParticleBench/main.swift"}


def test_setuid_bit_dropped_as_cheat_clue(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    src = tmp_path / "src"
    script = _write(src, "answer.json", "{}")
    script.chmod(script.stat().st_mode | stat.S_ISUID)
    export = sanitise_tree(src, tmp_path / "out", artifact_decls(pack))
    setuid = _issue(export, REASON_SETUID)
    assert [i.path for i in setuid] == ["answer.json"]
    assert setuid[0].cheat_clue
    assert export.artifacts == ()


def test_declared_max_bytes_drops_oversized_file(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")  # answer.json max_bytes 65536
    src = tmp_path / "src"
    _write(src, "answer.json", "x" * 70000)
    export = sanitise_tree(src, tmp_path / "out", artifact_decls(pack))
    too_large = _issue(export, REASON_TOO_LARGE)
    assert [i.path for i in too_large] == ["answer.json"]
    assert not too_large[0].cheat_clue  # a warning, not smuggling
    assert export.artifacts == ()


def test_count_cap_drops_in_sorted_order(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("macos-001")  # Sources/ParticleBench/ is one declared dir
    src = tmp_path / "src"
    for name in ("b.swift", "a.swift", "c.swift"):
        _write(src, f"Sources/ParticleBench/{name}", name)
    caps = ExportCaps(max_files=2, max_total_bytes=1 << 30, default_file_max_bytes=1 << 20)
    export = sanitise_tree(src, tmp_path / "out", artifact_decls(pack), caps=caps)
    assert [a.path for a in export.artifacts] == [
        "Sources/ParticleBench/a.swift",
        "Sources/ParticleBench/b.swift",
    ]
    too_many = _issue(export, REASON_TOO_MANY)
    assert [i.path for i in too_many] == ["Sources/ParticleBench/c.swift"]


def test_missing_required_artifact_recorded(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("logic-001")
    src = tmp_path / "src"
    _write(src, "instances.json", "{}")
    export = sanitise_tree(
        src, tmp_path / "out", artifact_decls(pack), protected=("instances.json",)
    )
    missing = _issue(export, REASON_MISSING)
    assert [i.path for i in missing] == ["answer.json"]
    assert {a.path for a in export.artifacts} == {"instances.json"}


def test_total_size_cap(tmp_path: Path, pack_factory) -> None:
    pack: TaskPack = pack_factory("macos-001")
    src = tmp_path / "src"
    _write(src, "Sources/ParticleBench/a.swift", "x" * 100)
    _write(src, "Sources/ParticleBench/b.swift", "x" * 100)
    caps = ExportCaps(max_files=16, max_total_bytes=150, default_file_max_bytes=1 << 20)
    export = sanitise_tree(src, tmp_path / "out", artifact_decls(pack), caps=caps)
    assert [a.path for a in export.artifacts] == ["Sources/ParticleBench/a.swift"]
    assert _issue(export, "total_size_cap")
