"""Sandbox conformance cases for declared paths and upload semantics.

C-PATH-01: paths a spec declares in ``writable_paths`` exist, empty, writable and executable
by ``run_as_user``, and are gone with the sandbox (carriers declaring ``writable_paths``).

C-PATH-02: ``upload(read_only=True)`` leaves a tree readable and executable but not writable,
appendable, deletable, renamable or chmod-able by ``run_as_user``, and its content unchanged
(carriers declaring ``read_only_upload``).

C-PATH-03: a carrier without one of those capabilities refuses it loudly: declared paths with
``PolicyNotEnforceable`` at create, a read-only upload with ``CapabilityNotSupported`` before
anything lands.

C-FILE-07: a directory uploaded onto an existing directory merges like a tar extraction:
entries at the same path are replaced, the others stay.

Checks run through ``exec`` with paths relative to ``cwd`` so every carrier resolves them in
the sandbox's own view.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from evalspleef.conformance.case import Case
from evalspleef.conformance.cases_sandbox_env import (
    _build_spec,
    _capabilities,
    _open_spec,
    _posix_gate,
    _provider,
    _sandbox_gate,
)
from evalspleef.conformance.cases_sandbox_roots import WORKSPACE, _sandbox_user, _upload_tree
from evalspleef.spi.caps import OSFamily
from evalspleef.spi.errors import CapabilityNotSupported, PolicyNotEnforceable
from evalspleef.spi.sandbox import ExecResult, Sandbox, WritablePath

if TYPE_CHECKING:
    from evalspleef.conformance.target import ConformanceTarget

_DECLARED = (WritablePath("/spleef-declared-a", 16), WritablePath("/var/spleef-declared-b", 16))
_RO_DIR = f"{WORKSPACE}/spleef-read-only"
_RO_FILES = {"data.txt": b"read-only data\n", "sub/inner.txt": b"inner\n"}
_RO_SCRIPT = b"#!/bin/sh\nprintf '%s' read-only-run\n"


def _describe(result: ExecResult) -> str:
    return f"exit {result.exit_code}, stdout {result.stdout!r}, stderr {result.stderr!r}"


# -- C-PATH-01 ----------------------------------------------------------------


def _path01_applies(target: ConformanceTarget) -> str | None:
    gate = _posix_gate(target)
    if gate is not None:
        return gate
    if not _capabilities(target).writable_paths:
        return "target does not declare writable_paths (C-PATH-03 checks the refusal)"
    return None


async def _run_path01(target: ConformanceTarget) -> None:
    spec = replace(_build_spec(target, "conf-path-01-a"), writable_paths=_DECLARED)
    async with _open_spec(target, spec) as sandbox:
        user = await _sandbox_user(sandbox)
        for declared in _DECLARED:
            empty = await sandbox.exec(["sh", "-c", "ls -A"], cwd=declared.path, user=user)
            assert empty.exit_code == 0 and not empty.stdout.strip(), (
                f"declared path {declared.path} is missing or not empty: {_describe(empty)}"
            )
            script = (
                "printf '#!/bin/sh\\nprintf declared-ok\\n' > run.sh && chmod 755 run.sh && "
                "printf data > data.txt && cat data.txt && ./run.sh"
            )
            used = await sandbox.exec(["sh", "-c", script], cwd=declared.path, user=user)
            assert used.exit_code == 0 and used.stdout == b"datadeclared-ok", (
                f"{sandbox.spec.run_as_user!r} cannot write, read and execute in the declared "
                f"path {declared.path}: {_describe(used)}"
            )
    fresh_spec = replace(_build_spec(target, "conf-path-01-b"), writable_paths=_DECLARED)
    async with _open_spec(target, fresh_spec) as fresh:
        for declared in _DECLARED:
            listing = await fresh.exec(["sh", "-c", "ls -A"], cwd=declared.path)
            assert listing.exit_code == 0 and not listing.stdout.strip(), (
                f"a fresh sandbox sees files in the declared path {declared.path}: "
                f"{_describe(listing)}"
            )


# -- C-PATH-02 ----------------------------------------------------------------


def _path02_applies(target: ConformanceTarget) -> str | None:
    gate = _posix_gate(target)
    if gate is not None:
        return gate
    if not _capabilities(target).read_only_upload:
        return "target does not declare read_only_upload (C-PATH-03 checks the refusal)"
    return None


def _write_read_only_tree(root: Path) -> Path:
    tree = root / "spleef-read-only"
    for name, content in _RO_FILES.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_bytes(content)
        os.chmod(tree / name, 0o644)
    (tree / "run.sh").write_bytes(_RO_SCRIPT)
    os.chmod(tree / "run.sh", 0o755)
    return tree


def _snapshot(tree: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(tree)): path.read_bytes()
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


_FORBIDDEN = (
    ("overwrite a file", "printf x > data.txt"),
    ("append to a file", "printf x >> data.txt"),
    ("create a file in the tree", "printf x > created.txt"),
    ("create a directory in the tree", "mkdir created-dir"),
    ("delete a file", "rm -f data.txt"),
    ("delete a nested file", "rm -f sub/inner.txt"),
    ("rename a file", "mv data.txt renamed.txt"),
    ("chmod a file", "chmod 666 data.txt"),
    ("chmod the tree", "chmod 777 ."),
)


async def _run_path02(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-path-02")) as sandbox:
        user = await _sandbox_user(sandbox)
        with tempfile.TemporaryDirectory(prefix="spleef-conf-path-02-") as tmp:
            tree = _write_read_only_tree(Path(tmp))
            await sandbox.upload(tree, _RO_DIR, read_only=True)
            expected = _snapshot(tree)
            read = await sandbox.exec(["sh", "-c", "cat data.txt && ./run.sh"], cwd=_RO_DIR)
            assert (
                read.exit_code == 0 and read.stdout == _RO_FILES["data.txt"] + b"read-only-run"
            ), f"a read-only upload is not readable and executable: {_describe(read)}"
            allowed = [
                what
                for what, command in _FORBIDDEN
                if (await sandbox.exec(["sh", "-c", command], cwd=_RO_DIR, user=user)).exit_code
                == 0
            ]
            assert not allowed, (
                f"{sandbox.spec.run_as_user!r} could {', '.join(allowed)} in a read-only upload"
            )
            back = Path(tmp) / "back"
            await sandbox.download(_RO_DIR, back)
            assert _snapshot(back) == expected, (
                f"the read-only tree changed: {sorted(_snapshot(back))} vs {sorted(expected)}"
            )


# -- C-PATH-03 ----------------------------------------------------------------


def _path03_applies(target: ConformanceTarget) -> str | None:
    # The refusal is the carrier's, so any guest OS applies; only the leftover probe is POSIX.
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if caps.writable_paths and caps.read_only_upload:
        return "target declares writable_paths and read_only_upload (C-PATH-01/02 apply)"
    return None


async def _run_path03(target: ConformanceTarget) -> None:
    caps = _capabilities(target)
    if not caps.writable_paths:
        spec = replace(_build_spec(target, "conf-path-03-a"), writable_paths=_DECLARED[:1])
        try:
            sandbox = await _provider(target).create(spec)
        except PolicyNotEnforceable:
            pass
        else:
            await sandbox.destroy()
            raise AssertionError(
                "create accepted declared writable paths although the target does not declare "
                "writable_paths; it must raise PolicyNotEnforceable"
            )
    if not caps.read_only_upload:
        async with _open_spec(target, _build_spec(target, "conf-path-03-b")) as sandbox:
            await _assert_read_only_refused(sandbox)


async def _assert_read_only_refused(sandbox: Sandbox) -> None:
    with tempfile.TemporaryDirectory(prefix="spleef-conf-path-03-") as tmp:
        source = Path(tmp, "refused.txt")
        await asyncio.to_thread(source.write_bytes, b"must not land\n")
        try:
            await sandbox.upload(source, f"{WORKSPACE}/refused.txt", read_only=True)
        except CapabilityNotSupported:
            pass
        else:
            raise AssertionError(
                "upload(read_only=True) succeeded although the target does not declare "
                "read_only_upload; it must raise CapabilityNotSupported"
            )
    if sandbox.spec.os in (OSFamily.LINUX, OSFamily.MACOS):
        landed = await sandbox.exec(["sh", "-c", "[ -e refused.txt ]"], cwd=WORKSPACE)
        assert landed.exit_code != 0, "a refused read-only upload still wrote its file"


# -- C-FILE-07 ----------------------------------------------------------------


async def _run_file07(target: ConformanceTarget) -> None:
    remote = f"{WORKSPACE}/spleef-merge"
    async with _open_spec(target, _build_spec(target, "conf-file-07")) as sandbox:
        with tempfile.TemporaryDirectory(prefix="spleef-conf-file-07-") as tmp:
            first, second = Path(tmp, "first"), Path(tmp, "second")
            for tree, own, shared in ((first, "kept.txt", b"old"), (second, "added.txt", b"new")):
                (tree / "sub").mkdir(parents=True)
                (tree / own).write_bytes(own.encode())
                (tree / "sub" / "shared.txt").write_bytes(shared)
            await _upload_tree(sandbox, first, remote)
            await _upload_tree(sandbox, second, remote)
        merged = await sandbox.exec(
            ["sh", "-c", "cat kept.txt added.txt sub/shared.txt"], cwd=remote
        )
    assert merged.exit_code == 0 and merged.stdout == b"kept.txtadded.txtnew", (
        "a directory uploaded onto an existing one must merge (sibling kept, same path "
        f"replaced): {_describe(merged)}"
    )


CASES: tuple[Case, ...] = (
    Case(
        id="C-PATH-01",
        kind="sandbox",
        title="declared writable paths are empty, usable by the sandbox user and per sandbox",
        applies=_path01_applies,
        run=_run_path01,
    ),
    Case(
        id="C-PATH-02",
        kind="sandbox",
        title="a read-only upload cannot be written, deleted, renamed or chmod-ed",
        applies=_path02_applies,
        run=_run_path02,
    ),
    Case(
        id="C-PATH-03",
        kind="sandbox",
        title="undeclared path capabilities are refused loudly",
        applies=_path03_applies,
        run=_run_path03,
    ),
    Case(
        id="C-FILE-07",
        kind="sandbox",
        title="a directory upload merges into an existing directory",
        applies=_posix_gate,
        run=_run_file07,
    ),
)
