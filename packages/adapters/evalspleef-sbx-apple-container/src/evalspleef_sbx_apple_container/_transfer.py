"""Local validation of downloaded artefacts (invariant I11).

``container cp`` hands the bytes to the host without interpretation, so the
provider validates the result locally before the caller sees it: no symlink may
escape the download root, no special file and no setuid/setgid bit survives.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from evalspleef.spi.errors import TransferError


def validate_download(root: Path, *, max_bytes: int | None = None) -> None:
    """Reject an unsafe or oversized downloaded tree in place."""
    if root.is_symlink():
        _check_symlink(root, root.parent)
        raise TransferError(f"download root {root} is a symlink")
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise TransferError(f"downloaded path {current} vanished") from exc
        if stat.S_ISLNK(info.st_mode):
            _check_symlink(current, root)
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise TransferError(f"downloaded artefact {current} is not a regular file or directory")
        if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            current.chmod(stat.S_IMODE(info.st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
        if stat.S_ISDIR(info.st_mode):
            try:
                stack.extend(current.iterdir())
            except OSError as exc:
                raise TransferError(f"cannot list downloaded directory {current}: {exc}") from exc
            continue
        total += info.st_size
        if max_bytes is not None and total > max_bytes:
            raise TransferError(f"download exceeds the {max_bytes} byte limit")


def _check_symlink(link: Path, root: Path) -> None:
    target = os.readlink(link)
    if os.path.isabs(target):
        raise TransferError(f"download contains an absolute symlink {link} -> {target}")
    resolved = (link.parent / target).resolve(strict=False)
    base = root.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise TransferError(f"download contains a symlink escaping the root: {link} -> {target}")
