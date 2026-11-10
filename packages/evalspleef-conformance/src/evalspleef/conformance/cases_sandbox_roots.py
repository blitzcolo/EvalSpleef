"""Sandbox conformance cases for the writable roots the platform uploads into.

C-FILE-05: the trial workspace is staged by uploading a whole directory onto
``/workspace``, which carriers usually provide as a mount point (a tmpfs or a volume
owned by root). The directory's entries must land at ``/workspace/<name>``, visible to
``exec``, byte-identical, with their executable bits, and readable and writable by the
sandbox user; a second fresh sandbox must not see them.

C-FILE-06: a ``role="verifier"`` sandbox receives the hidden tests and the exported
workspace under the verify root and runs them from there, so that root must accept a
directory upload, execute a script from it and let the script write next to it. The
root is the carrier's declared ``capabilities().verify_root`` unless the operator sets
``SPLEEF_VERIFY_ROOT``, and it is declared as a writable path on carriers that mount
them, as the control plane's lease manager does; no adapter may key on the role.

Every check goes through ``exec`` rather than ``download``: a carrier whose transfer
path writes underneath a mount point would pass a download round trip while the
sandboxed process sees nothing.
"""

from __future__ import annotations

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
)
from evalspleef.spi.errors import TransferError
from evalspleef.spi.sandbox import ExecResult, Sandbox, SandboxCapabilities, WritablePath

if TYPE_CHECKING:
    from evalspleef.conformance.target import ConformanceTarget

WORKSPACE = "/workspace"
VERIFY_ROOT_ENV = "SPLEEF_VERIFY_ROOT"
_VERIFY_ROOT_SIZE_MB = 64

_TREE_NAME = "spleef-mount-tree"
"""Local name of the uploaded directory; only its entries belong in the workspace."""
_PAYLOAD = bytes(range(256)) * 2 + b"\r\n\x00mount-point-tail"
_NESTED = b"nested entry\n"
_RUN_OUTPUT = b"run-from-mount-point"
_TOOL_OUTPUT = b"nested-tool"
_PLACED = ("data.bin", "run.sh", "sub/nested.txt", "sub/tool.sh")


def verify_root(caps: SandboxCapabilities) -> str:
    """The verify root the control plane uses: the operator's ``SPLEEF_VERIFY_ROOT``, else
    the carrier's declared ``verify_root``."""
    return os.environ.get(VERIFY_ROOT_ENV) or caps.verify_root


def _verifier_paths(root: str, caps: SandboxCapabilities) -> tuple[WritablePath, ...]:
    """The lease manager's rule: declare the root unless it lies in the workspace or the
    carrier cannot mount paths (then its declared root must already be writable)."""
    inside_workspace = root == WORKSPACE or root.startswith(f"{WORKSPACE}/")
    if caps.writable_paths and not inside_workspace:
        return (WritablePath(root, _VERIFY_ROOT_SIZE_MB),)
    return ()


def _text(result: ExecResult) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def _script(output: bytes) -> bytes:
    return b"#!/bin/sh\nprintf '%s' '" + output + b"'\n"


async def _sandbox_user(sandbox: Sandbox) -> str | None:
    """``user=`` for exec as ``spec.run_as_user``; None when exec already runs as it or the
    image has no such account (then the default user is all a case can exercise)."""
    expected = sandbox.spec.run_as_user
    current = await sandbox.exec(["id", "-un"])
    if current.exit_code == 0 and _text(current) == expected:
        return None
    account = await sandbox.exec(["id", "-u", expected])
    return expected if account.exit_code == 0 else None


async def _check(sandbox: Sandbox, script: str, cwd: str, what: str) -> ExecResult:
    result = await sandbox.exec(["sh", "-c", script], cwd=cwd)
    assert result.exit_code == 0, (
        f"{what}: exit {result.exit_code}, stdout {result.stdout!r}, stderr {result.stderr!r}"
    )
    return result


async def _upload_tree(sandbox: Sandbox, tree: Path, remote: str) -> None:
    try:
        await sandbox.upload(tree, remote)
    except TransferError as exc:
        raise AssertionError(f"uploading a directory to {remote} failed: {exc}") from exc


def _write_mount_tree(root: Path) -> Path:
    tree = root / _TREE_NAME
    (tree / "sub").mkdir(parents=True)
    (tree / "data.bin").write_bytes(_PAYLOAD)
    (tree / "run.sh").write_bytes(_script(_RUN_OUTPUT))
    (tree / "sub" / "nested.txt").write_bytes(_NESTED)
    (tree / "sub" / "tool.sh").write_bytes(_script(_TOOL_OUTPUT))
    modes = (
        ("data.bin", 0o644),
        ("run.sh", 0o755),
        ("sub/nested.txt", 0o644),
        ("sub/tool.sh", 0o755),
    )
    for name, mode in modes:
        os.chmod(tree / name, mode)
    return tree


# -- C-FILE-05 ----------------------------------------------------------------


async def _run_file05(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-file-05-a")) as sandbox:
        with tempfile.TemporaryDirectory(prefix="spleef-conf-file-05-") as tmp:
            await _upload_tree(sandbox, _write_mount_tree(Path(tmp)), WORKSPACE)
        await _assert_tree_in_workspace(sandbox)
        await _assert_writable_by_user(sandbox)
    async with _open_spec(target, _build_spec(target, "conf-file-05-b")) as fresh:
        leftovers = await _check(
            fresh,
            'for p in data.bin run.sh sub; do if [ -e "$p" ]; then echo "$p"; fi; done',
            WORKSPACE,
            "probing the fresh workspace",
        )
    assert not leftovers.stdout.strip(), (
        f"a fresh sandbox sees entries uploaded into another one: {leftovers.stdout!r}"
    )


async def _assert_tree_in_workspace(sandbox: Sandbox) -> None:
    listing = await _check(
        sandbox,
        "for p in " + " ".join(_PLACED) + ' ; do if [ -f "$p" ]; then echo "$p"; fi; done',
        WORKSPACE,
        "listing the uploaded entries",
    )
    present = set(_text(listing).split())
    missing = sorted(set(_PLACED) - present)
    assert not missing, (
        f"entries of a directory uploaded to {WORKSPACE} are not at {WORKSPACE}/<name>: "
        f"missing {missing}"
    )
    for name, expected in (("data.bin", _PAYLOAD), ("sub/nested.txt", _NESTED)):
        # A hex dump, because exec output is text on some carriers; od is POSIX.
        dump = await sandbox.exec(["od", "-An", "-v", "-tx1", name], cwd=WORKSPACE)
        observed = _text(dump).split()
        assert dump.exit_code == 0 and observed == expected.hex(" ").split(), (
            f"{WORKSPACE}/{name} differs from the uploaded bytes: exit {dump.exit_code}, "
            f"{len(observed)} bytes instead of {len(expected)}, stderr {dump.stderr!r}"
        )
    for name, output in (("./run.sh", _RUN_OUTPUT), ("./sub/tool.sh", _TOOL_OUTPUT)):
        ran = await sandbox.exec([name], cwd=WORKSPACE)
        assert ran.exit_code == 0 and ran.stdout == output, (
            f"{WORKSPACE}/{name} lost its executable bit or content: exit {ran.exit_code}, "
            f"stdout {ran.stdout!r}, stderr {ran.stderr!r}"
        )
    await _check(
        sandbox, "[ ! -x data.bin ] && [ ! -x sub/nested.txt ]", WORKSPACE, "mode bits of data"
    )


async def _assert_writable_by_user(sandbox: Sandbox) -> None:
    user = await _sandbox_user(sandbox)
    who = sandbox.spec.run_as_user
    script = (
        "[ -r data.bin ] && [ -w data.bin ] && printf more >> data.bin "
        "&& printf created > sub/created.txt && printf top > top-level.txt "
        "&& cat sub/created.txt top-level.txt"
    )
    wrote = await sandbox.exec(["sh", "-c", script], cwd=WORKSPACE, user=user)
    assert wrote.exit_code == 0 and wrote.stdout == b"createdtop", (
        f"uploaded entries in {WORKSPACE} are not readable and writable by {who!r}: "
        f"exit {wrote.exit_code}, stdout {wrote.stdout!r}, stderr {wrote.stderr!r}"
    )


# -- C-FILE-06 ----------------------------------------------------------------


_FIXTURE = b"fixture-from-hidden-tests\n"
_ANSWER = b"answer-from-workspace\n"
_VERIFY_RUNNER = (
    b"#!/bin/sh\nset -e\nprintf verified > ../verifier-wrote.txt\n"
    b"cat fixture.txt ../workspace/answer.txt\n"
)


def _write_verify_trees(root: Path) -> tuple[Path, Path]:
    """The ``hidden-tests`` and ``workspace`` trees the evaluator uploads to a verifier."""
    hidden, workspace = root / "hidden-tests", root / "workspace"
    hidden.mkdir()
    workspace.mkdir()
    (hidden / "run.sh").write_bytes(_VERIFY_RUNNER)
    os.chmod(hidden / "run.sh", 0o755)
    (hidden / "fixture.txt").write_bytes(_FIXTURE)
    (workspace / "answer.txt").write_bytes(_ANSWER)
    return hidden, workspace


async def _run_file06(target: ConformanceTarget) -> None:
    caps = _capabilities(target)
    root = verify_root(caps)
    spec = replace(
        _build_spec(target, "conf-file-06"),
        role="verifier",
        writable_paths=_verifier_paths(root, caps),
    )
    async with _open_spec(target, spec) as sandbox:
        with tempfile.TemporaryDirectory(prefix="spleef-conf-file-06-") as tmp:
            hidden, workspace = _write_verify_trees(Path(tmp))
            await _upload_tree(sandbox, hidden, f"{root}/hidden-tests")
            await _upload_tree(sandbox, workspace, f"{root}/workspace")
        ran = await sandbox.exec(["./run.sh"], cwd=f"{root}/hidden-tests")
        assert ran.exit_code == 0 and ran.stdout == _FIXTURE + _ANSWER, (
            f"a verifier sandbox cannot run a script from {root}/hidden-tests: "
            f"exit {ran.exit_code}, stdout {ran.stdout!r}, stderr {ran.stderr!r}"
        )
        wrote = await sandbox.exec(["cat", "verifier-wrote.txt"], cwd=root)
        assert wrote.exit_code == 0 and wrote.stdout == b"verified", (
            f"a file the verifier wrote under {root} is not there: exit {wrote.exit_code}, "
            f"stdout {wrote.stdout!r}, stderr {wrote.stderr!r}"
        )
        created = await sandbox.exec(
            ["sh", "-c", "printf new > new-file.txt && cat new-file.txt"],
            cwd=root,
        )
        assert created.exit_code == 0 and created.stdout == b"new", (
            f"the verify root {root} does not accept a new file: exit {created.exit_code}, "
            f"stderr {created.stderr!r}"
        )


CASES: tuple[Case, ...] = (
    Case(
        id="C-FILE-05",
        kind="sandbox",
        title="a directory uploaded onto the workspace mount point fills it",
        applies=_posix_gate,
        run=_run_file05,
    ),
    Case(
        id="C-FILE-06",
        kind="sandbox",
        title="a verifier sandbox gets a writable, executable verify root",
        applies=_posix_gate,
        run=_run_file06,
    ),
)
