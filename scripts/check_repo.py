#!/usr/bin/env python3
"""Repository consistency check: layout contract, relative links, schema refs, runnable tests.

Usage:
    python3 scripts/check_repo.py            # structure + links + schema checks
    python3 scripts/check_repo.py --run      # also run core unit tests and task-pack selftests

Exit codes: 0 = all checks pass; 1 = at least one error; 2 = warnings only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ERRORS: list[str] = []
WARNINGS: list[str] = []

REQUIRED_DOCS = [
    "docs/00-conventions.md",
    "docs/01-architecture.md",
    "docs/02-eval-flow.md",
    "docs/03-grading.md",
    "docs/04-isolation.md",
    "docs/05-task-library.md",
    "docs/06-answer-and-acceptance.md",
    "docs/07-harness-mapping.md",
    "docs/08-implementation-notes.md",
]

REQUIRED_SCHEMAS = [
    "schemas/task.schema.json",
    "schemas/rubric.schema.json",
    "schemas/defect.schema.json",
    "schemas/feedback.schema.json",
    "schemas/grade.schema.json",
    "schemas/trial.schema.json",
]

TASK_PACKAGE_FILES = [
    "task.yaml",
    "instruction.md",
    "persona.md",
    "rubric.yaml",
    "defects.yaml",
    "answers/empty-agent.md",
    "hidden-tests/run.sh",
    "canary.md",
    "notes.md",
]

# Only these trees are owned by this check; packages may be mid-edit by other lanes.
LINK_ROOTS = ("docs", "tasks")
TASKS_DIR = ROOT / "tasks"
CORE_DIR = ROOT / "packages" / "evalspleef-core"

LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def check_required_files() -> None:
    for rel in REQUIRED_DOCS:
        if not (ROOT / rel).is_file():
            err(f"missing required document: {rel}")
    for rel in REQUIRED_SCHEMAS:
        p = ROOT / rel
        if not p.is_file():
            err(f"missing required schema: {rel}")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            err(f"schema is not valid JSON: {rel} ({exc})")
            continue
        expected = f"https://llm-eval.local/schemas/{p.name}"
        if data.get("$id") != expected:
            err(f"schema $id mismatch: {rel} expected {expected}, got {data.get('$id')}")
        for key in ("$schema", "title", "type", "properties"):
            if key not in data:
                warn(f"schema is missing {key}: {rel}")
        for ref in find_refs(data):
            if ref.startswith("#") or "://" in ref:
                continue
            target = (p.parent / ref.split("#")[0]).resolve()
            if not target.is_file():
                err(f"schema reference does not exist: {rel} -> {ref}")


def find_refs(node: object) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                refs.append(v)
            else:
                refs.extend(find_refs(v))
    elif isinstance(node, list):
        for item in node:
            refs.extend(find_refs(item))
    return refs


def check_task_packages() -> None:
    if not TASKS_DIR.is_dir():
        err("missing tasks/ directory")
        return
    packages = sorted(p for p in TASKS_DIR.iterdir() if p.is_dir())
    if not packages:
        err("tasks/ contains no task packs")
    for pkg in packages:
        for rel in TASK_PACKAGE_FILES:
            if not (pkg / rel).is_file():
                err(f"task pack is missing a file: {pkg.name}/{rel}")
        for letter in ("a", "b"):
            found = any((pkg / "answers").glob(f"reference-{letter}.*"))
            if not found:
                err(
                    f"task pack is missing a reference solution: {pkg.name}/answers/reference-{letter}.*"
                )
        # Hidden tests must not leak into agent-visible files (task.yaml references them
        # as hard-gate entry points, which is expected for control-plane metadata).
        for visible in ("instruction.md",):
            f = pkg / visible
            if f.is_file() and "hidden-tests" in f.read_text(encoding="utf-8"):
                warn(f"{pkg.name}/{visible} mentions hidden-tests; check for a leak")
        ws = pkg / "workspace"
        if ws.is_dir():
            for f in ws.rglob("*"):
                if f.is_file() and "hidden-tests" in str(f):
                    warn(f"{pkg.name}/workspace contains a hidden-tests path: {f.relative_to(pkg)}")


def iter_link_sources():
    for root_name in LINK_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for md in sorted(base.rglob("*.md")):
            if any(part in (".git", ".venv", "node_modules") for part in md.parts):
                continue
            yield md


def check_links() -> None:
    for md in iter_link_sources():
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            target = m.group(2).split("#")[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if target.startswith("/"):
                err(f"absolute link should be relative: {md.relative_to(ROOT)} -> {m.group(2)}")
                continue
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                err(f"broken link: {md.relative_to(ROOT)} -> {m.group(2)}")


def check_enums() -> None:
    """Spot-check that tier spellings stay unified (no Bplus/Cplus variants)."""
    tiers = {"P", "A", "B+", "B", "C+", "C", "D", "F"}
    for schema in ("schemas/grade.schema.json", "schemas/trial.schema.json"):
        p = ROOT / schema
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        found: set[str] = set()

        def walk(node: object, key: str = "", bucket: set[str] = found) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "enum" and isinstance(v, list) and set(v) >= tiers - {"P", "F"}:
                        bucket.update(x for x in v if x in tiers)
                    walk(v, k)
            elif isinstance(node, list):
                for i in node:
                    walk(i, key)

        walk(data)
        if found and found != tiers:
            warn(f"{schema} has an incomplete tier enum: {sorted(found)}")


def _python_for_packages() -> str:
    """Prefer the workspace interpreter so evalspleef.core is importable."""
    venv_python = ROOT / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.is_file() else sys.executable


def run_tests() -> None:
    tests = CORE_DIR / "tests"
    if tests.is_dir():
        cmd = [
            _python_for_packages(),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=CORE_DIR)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        print(f"[run] core unit tests: exit={proc.returncode} | " + " / ".join(tail))
        if proc.returncode != 0:
            err("core unit tests failed")

    if not TASKS_DIR.is_dir():
        return
    for pkg in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
        runner = pkg / "hidden-tests" / "run.sh"
        if not (runner.is_file() and os.access(runner, os.X_OK)):
            warn(f"task pack hidden-test script missing or not executable: {pkg.name}")
            continue
        # run.sh semantics: default checks the candidate tree; --selftest proves the pack
        # itself (references pass / starter fails / hash locks hold). The repository check
        # uses --selftest and runs on a copy because selftests write temporary files.
        try:
            has_selftest = "--selftest" in runner.read_text(encoding="utf-8", errors="replace")
        except OSError:
            has_selftest = False
        selftest_arg = ["--selftest"] if has_selftest else []
        with tempfile.TemporaryDirectory(prefix=f"check-repo-{pkg.name}-") as tmp:
            copy = Path(tmp) / pkg.name
            shutil.copytree(pkg, copy)
            copy_runner = copy / "hidden-tests" / "run.sh"
            cmd = ["bash", str(copy_runner), *selftest_arg]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=copy, timeout=600)
            except subprocess.TimeoutExpired:
                err(f"task pack hidden tests timed out: {pkg.name}")
                continue
        print(
            f"[run] {pkg.name} hidden-tests{' --selftest' if has_selftest else ''}: "
            f"exit={proc.returncode}"
        )
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            err(f"task pack hidden tests failed: {pkg.name} ({' / '.join(tail)})")


def check_todos() -> None:
    counts = {"TODO(verify)": 0, "TODO(run-on-host)": 0, "TODO": 0}
    for md in iter_link_sources():
        text = md.read_text(encoding="utf-8", errors="replace")
        if "TODO(verify)" in text:
            counts["TODO(verify)"] += text.count("TODO(verify)")
        if "TODO(run-on-host)" in text:
            counts["TODO(run-on-host)"] += text.count("TODO(run-on-host)")
        counts["TODO"] += text.count("TODO(")
    print(
        f"[info] TODO(verify)={counts['TODO(verify)']} "
        f"TODO(run-on-host)={counts['TODO(run-on-host)']}"
    )
    if counts["TODO(verify)"] > 20:
        warn("more than 20 TODO(verify) markers; evidence chain is thin")


def main() -> int:
    parser = argparse.ArgumentParser(description="EvalSpleef repository check")
    parser.add_argument("--run", action="store_true", help="run core tests and pack selftests")
    args = parser.parse_args()

    check_required_files()
    check_task_packages()
    check_links()
    check_enums()
    check_todos()
    if args.run:
        run_tests()

    for w in WARNINGS:
        print(f"[WARN] {w}")
    for e in ERRORS:
        print(f"[ERROR] {e}")
    print(f"\nresult: {len(ERRORS)} error / {len(WARNINGS)} warning")
    if ERRORS:
        return 1
    return 0 if not WARNINGS else 2


if __name__ == "__main__":
    raise SystemExit(main())
