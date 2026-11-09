"""Tar streams for file transfer and local validation of downloads (invariant I11).

Uploads are packed on the host and unpacked by ``tar`` inside the sandbox;
downloads are packed by ``tar`` inside the sandbox and unpacked here. The guest
controls the download stream, so extraction refuses absolute or escaping names
and links and special files, strips setuid/setgid bits, and the extracted tree
is validated again before it is moved to the caller's path.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import IO

from evalspleef.spi.errors import TransferError

_HIGH_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


def build_upload_tar(local: Path, *, arcname: str, contents_only: bool = False) -> bytes:
    """Pack ``local`` (file, directory or symlink) as a tar whose top entry is ``arcname``.

    Modes and mtimes are kept; ownership is dropped because the sandbox user
    unpacks the stream and owns what it writes. With ``contents_only`` a directory
    is packed without its own entry, so unpacking it never touches the metadata of
    the destination directory (a root-owned tmpfs mount point such as /workspace).
    """
    if not local.exists() and not local.is_symlink():
        raise TransferError(f"local path does not exist: {local}")

    def anonymise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (info.isreg() or info.isdir() or info.issym()):
            raise TransferError(f"cannot upload special file {info.name!r}")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        if contents_only and local.is_dir() and not local.is_symlink():
            for child in sorted(local.iterdir()):
                archive.add(str(child), arcname=child.name, recursive=True, filter=anonymise)
        else:
            archive.add(str(local), arcname=arcname, recursive=True, filter=anonymise)
    return buffer.getvalue()


def extract_download_tar(stream: IO[bytes], local: Path, *, max_bytes: int | None) -> None:
    """Unpack a guest ``tar -cf - -C <parent> ./<name>`` stream so ``local`` is ``<name>``."""
    local.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".spleef-download-", dir=local.parent))
    try:
        try:
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                tops = {_top_component(member.name) for member in archive.getmembers()}
                if len(tops) != 1:
                    raise TransferError(f"download archive has {len(tops)} top-level entries")
                archive.extractall(staging, filter=_safe_member)
        except tarfile.TarError as exc:
            raise TransferError(f"cannot unpack the download stream: {exc}") from exc
        extracted = staging / tops.pop()
        validate_download(extracted, max_bytes=max_bytes)
        _place(extracted, local)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _top_component(name: str) -> str:
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise TransferError(f"download archive member has no name: {name!r}")
    return parts[0]


def _safe_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    """``tarfile.data_filter`` checks (it also drops ownership), keeping the member's
    permission bits minus setuid/setgid/sticky."""
    try:
        checked = tarfile.data_filter(member, destination)
    except tarfile.FilterError as exc:
        raise TransferError(f"refusing unsafe download member {member.name!r}: {exc}") from exc
    return checked.replace(mode=member.mode & ~_HIGH_MODE_BITS, deep=False)


def _place(extracted: Path, local: Path) -> None:
    if extracted.is_dir() and not extracted.is_symlink() and local.is_dir():
        shutil.copytree(extracted, local, symlinks=True, dirs_exist_ok=True)
        return
    if local.is_symlink() or local.is_file():
        local.unlink()
    elif local.exists():
        raise TransferError(f"cannot replace directory {local} with a file download")
    os.replace(extracted, local)


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
