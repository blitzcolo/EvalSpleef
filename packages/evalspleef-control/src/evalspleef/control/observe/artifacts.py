"""Artifact export and sanitising for the verifier (PRD appendix D.2, invariant I11).

The agent's workspace is exported from the agent sandbox, sanitised, hashed and stored
locally; the sanitised tree is what gets uploaded to the independent verifier sandbox
(see :mod:`evalspleef.control.observe.verifier`) and what judge prompts are built from.

Sanitising rules (I11: no ``type ∈ {symlink, fifo, socket, device}``, no setuid/setgid
bits, size and count caps, every surviving file carries a sha256):

- any non-regular file (symlink, fifo, socket, device) is dropped and recorded;
- any regular file with a setuid or setgid bit is dropped and recorded — trying to move
  privileged content into the verification side is a cheat clue (夹带), not just noise;
- a file larger than its declared ``max_bytes`` (or the default cap) is dropped;
- export-wide caps on file count and total bytes apply in deterministic path order;
- every surviving file is copied as plain content and gets a sha256 in its record.

Only content covered by a declared artifact or a pack protected path is exported,
so a candidate cannot smuggle judge-facing instructions through unlisted files.
Packs that declare no ``artifacts:`` block at all (debug-001/backend-001 style,
whose hard gates run against the whole project directory) export the full
workspace instead — the I11 caps and hashing apply unchanged and the verifier
candidate is the complete sanitised tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from evalspleef.spi.errors import TransferError
from evalspleef.spi.sandbox import Sandbox

from ..taskpack.models import TaskPack
from ..trial.types import Artifact

__all__ = [
    "REASON_SETUID",
    "REASON_SYMLINK",
    "ArtifactDecl",
    "ExportCaps",
    "SanitizeIssue",
    "WorkspaceExport",
    "artifact_decls",
    "export_workspace",
    "protected_paths",
    "sanitise_tree",
]

#: Default per-file cap when the pack declares no ``max_bytes`` for an artifact.
DEFAULT_FILE_MAX_BYTES = 64 << 20

REASON_SYMLINK = "symlink"
REASON_FIFO = "fifo"
REASON_SOCKET = "socket"
REASON_DEVICE = "device"
REASON_SETUID = "setuid_bit"
REASON_TOO_LARGE = "file_too_large"
REASON_TOO_MANY = "file_count_cap"
REASON_TOTAL_CAP = "total_size_cap"
REASON_MISSING = "artifact_missing"


@dataclass(frozen=True)
class ArtifactDecl:
    """One ``artifacts:`` entry of task.yaml."""

    path: str
    kind: str = ""
    required: bool = False
    max_bytes: int | None = None
    description: str = ""


@dataclass(frozen=True)
class ExportCaps:
    """Export-wide limits applied after per-file checks (I11 size/count caps)."""

    max_files: int = 4096
    max_total_bytes: int = 1 << 30
    default_file_max_bytes: int = DEFAULT_FILE_MAX_BYTES


@dataclass(frozen=True)
class SanitizeIssue:
    """One dropped or missing export entry; ``cheat_clue`` marks smuggling attempts."""

    path: str
    reason: str
    cheat_clue: bool = False
    detail: str = ""


@dataclass(frozen=True)
class WorkspaceExport:
    """The sanitised workspace tree plus the declared-artifact records."""

    tree: Path
    """Local directory holding the sanitised tree (workspace-relative layout)."""
    artifacts: tuple[Artifact, ...]
    issues: tuple[SanitizeIssue, ...]
    file_count: int
    total_bytes: int

    @property
    def cheat_clues(self) -> tuple[SanitizeIssue, ...]:
        """Dropped content that counts as a cheat signal rather than a warning."""
        return tuple(issue for issue in self.issues if issue.cheat_clue)


def artifact_decls(pack: TaskPack) -> tuple[ArtifactDecl, ...]:
    """Parse the ``artifacts:`` block of task.yaml."""
    raw = pack.metadata.get("artifacts")
    if not isinstance(raw, list):
        return ()
    decls: list[ArtifactDecl] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path", "")).strip("/")
        if not path:
            continue
        max_bytes = entry.get("max_bytes")
        decls.append(
            ArtifactDecl(
                path=path,
                kind=str(entry.get("kind", "")),
                required=bool(entry.get("required", False)),
                max_bytes=int(max_bytes) if isinstance(max_bytes, int | float) else None,
                description=str(entry.get("description", "")),
            )
        )
    return tuple(decls)


def protected_paths(pack: TaskPack) -> tuple[str, ...]:
    """The ``workspace.protected_paths`` of task.yaml (hash-locked candidate paths, I12)."""
    workspace = pack.metadata.get("workspace")
    if not isinstance(workspace, Mapping):
        return ()
    raw = workspace.get("protected_paths")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip("/") for item in raw if str(item).strip("/"))


def _decl_for(decls: Mapping[str, ArtifactDecl], relpath: str) -> ArtifactDecl | None:
    """The most specific declaration covering ``relpath`` (file itself or a parent dir)."""
    path = PurePosixPath(relpath)
    candidates = [
        decl
        for raw, decl in decls.items()
        if path == PurePosixPath(raw) or PurePosixPath(raw) in path.parents
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda decl: len(decl.path))


def _classify(mode: int) -> str | None:
    """I11 drop reason for a non-regular file mode, or ``None`` when regular."""
    if stat.S_ISLNK(mode):
        return REASON_SYMLINK
    if stat.S_ISFIFO(mode):
        return REASON_FIFO
    if stat.S_ISSOCK(mode):
        return REASON_SOCKET
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return REASON_DEVICE
    return None


def _covered(relpath: str, roots: tuple[str, ...]) -> bool:
    path = PurePosixPath(relpath)
    return any(relpath == root or PurePosixPath(root) in path.parents for root in roots)


def sanitise_tree(
    src: Path,
    dest: Path,
    decls: tuple[ArtifactDecl, ...],
    *,
    protected: tuple[str, ...] = (),
    caps: ExportCaps | None = None,
) -> WorkspaceExport:
    """Copy covered content of ``src`` into ``dest`` applying the I11 rules.

    Pure local operation: walk in sorted order, drop what the invariant forbids, cap
    count and volume, and hash every survivor. Files outside declared artifacts and
    protected paths are left behind (the verifier never sees undeclared content);
    when the pack declares no artifacts at all, the whole workspace is the candidate
    and everything is covered.
    """
    caps = caps or ExportCaps()
    by_path = {decl.path: decl for decl in decls}
    no_decls = not decls
    cover_roots = (*(decl.path for decl in decls), *protected)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    issues: list[SanitizeIssue] = []
    records: list[Artifact] = []
    total = 0
    count = 0

    for missing in decls:
        if (
            missing.required
            and not (src / missing.path).exists()
            and not (src / missing.path).is_symlink()
        ):
            issues.append(
                SanitizeIssue(path=missing.path, reason=REASON_MISSING, detail="required artifact")
            )

    for root, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            source = Path(root) / name
            relpath = source.relative_to(src).as_posix()
            if not no_decls and not _covered(relpath, cover_roots):
                continue
            try:
                info = source.lstat()
            except OSError as exc:
                issues.append(SanitizeIssue(path=relpath, reason="stat_failed", detail=str(exc)))
                continue
            kind = _classify(info.st_mode)
            if kind is not None:
                issues.append(SanitizeIssue(path=relpath, reason=kind, cheat_clue=True))
                continue
            if not stat.S_ISREG(info.st_mode):
                issues.append(SanitizeIssue(path=relpath, reason=REASON_DEVICE, cheat_clue=True))
                continue
            if info.st_mode & 0o6000:
                issues.append(
                    SanitizeIssue(
                        path=relpath,
                        reason=REASON_SETUID,
                        cheat_clue=True,
                        detail=oct(stat.S_IMODE(info.st_mode)),
                    )
                )
                continue
            decl = _decl_for(by_path, relpath)
            file_cap = (
                decl.max_bytes
                if decl is not None and decl.max_bytes is not None
                else caps.default_file_max_bytes
            )
            if info.st_size > file_cap:
                issues.append(
                    SanitizeIssue(
                        path=relpath, reason=REASON_TOO_LARGE, detail=f"{info.st_size} > {file_cap}"
                    )
                )
                continue
            if count >= caps.max_files:
                issues.append(SanitizeIssue(path=relpath, reason=REASON_TOO_MANY))
                continue
            if total + info.st_size > caps.max_total_bytes:
                issues.append(SanitizeIssue(path=relpath, reason=REASON_TOTAL_CAP))
                continue
            target = dest / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            count += 1
            total += info.st_size
            records.append(
                Artifact(path=relpath, sha256=digest, size_bytes=info.st_size, local_path=target)
            )

    return WorkspaceExport(
        tree=dest,
        artifacts=tuple(sorted(records, key=lambda item: item.path)),
        issues=tuple(issues),
        file_count=count,
        total_bytes=total,
    )


async def export_workspace(
    sandbox: Sandbox,
    remote_root: str,
    pack: TaskPack,
    dest: Path,
    *,
    caps: ExportCaps | None = None,
) -> WorkspaceExport:
    """Download the agent workspace and sanitise it for the verifier side.

    The whole workspace is downloaded in one call and re-validated locally (the SPI
    contract requires callers to re-validate downloads, I11). Download or sandbox
    failures propagate as the typed SPI errors; a missing workspace root yields an
    empty export with an issue, so a wiped workspace fails the gates honestly instead
    of failing the trial as infrastructure.
    """
    raw = dest.parent / f"{dest.name}-raw"
    if raw.exists():
        shutil.rmtree(raw)
    (raw / "workspace").mkdir(parents=True)
    try:
        await sandbox.download(remote_root, raw / "workspace")
    except TransferError as exc:
        if "does not exist" not in str(exc):
            shutil.rmtree(raw, ignore_errors=True)
            raise
    try:
        return sanitise_tree(
            raw / "workspace",
            dest,
            artifact_decls(pack),
            protected=protected_paths(pack),
            caps=caps,
        )
    finally:
        shutil.rmtree(raw, ignore_errors=True)
