"""Zip construction and sanitised extraction for guest file transfer.

Windows guests have no tar in the base image; directories travel as zip
archives moved over SFTP plus ``Compress-Archive``/``Expand-Archive`` inside
the guest (tar.exe works too but PowerShell cmdlets exist on every supported
build). Upload packs a local tree with the top-level entry named after the
remote basename; download applies the artifact sanitisation of PRD appendix
D.2 I11: members may not escape the export root, sizes are capped and
setuid/setgid bits are stripped on extraction.
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path

from evalspleef.spi.errors import TransferError

_MAX_MEMBER_BYTES = 1 << 30
"""Refuse a single zip member larger than 1 GiB before touching disk."""


def build_upload_zip(local: Path, *, arcname: str) -> bytes:
    """Pack ``local`` into a zip archive whose top-level entry is ``arcname``.

    Permission bits are stored in the zip external attributes so a Linux guest
    (which can also read zips through ``tar.exe`` or Python) keeps them;
    symlinks inside an uploaded tree are kept only when they stay inside the
    tree, otherwise the upload is rejected.
    """
    if not local.exists() and not local.is_symlink():
        raise TransferError(f"local path does not exist: {local}")
    _check_arcname(arcname)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if local.is_symlink():
            _add_symlink(archive, local, arcname, root=local.parent)
        elif local.is_dir():
            _add_directory(archive, local, arcname)
        else:
            _add_file(archive, local, arcname)
    return buffer.getvalue()


def _check_arcname(arcname: str) -> None:
    if not arcname or arcname.startswith("/") or ".." in Path(arcname).parts:
        raise TransferError(f"unsafe archive member name: {arcname!r}")


def _external_attributes(mode: int) -> int:
    return (mode & 0xFFFF) << 16


def _add_file(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    _check_arcname(arcname)
    if not path.is_file():
        raise TransferError(f"only regular files can be uploaded: {path}")
    info = zipfile.ZipInfo(arcname)
    info.date_time = datetime.fromtimestamp(path.stat().st_mtime).timetuple()[:6]
    info.external_attr = _external_attributes(path.stat().st_mode)
    with path.open("rb") as handle:
        archive.writestr(info, handle.read())


def _add_directory(archive: zipfile.ZipFile, root: Path, arcname: str) -> None:
    _check_arcname(arcname)
    if not root.is_dir():
        raise TransferError(f"not a directory: {root}")
    info = zipfile.ZipInfo(arcname + "/")
    info.external_attr = _external_attributes(root.stat().st_mode | stat.S_IFDIR)
    archive.writestr(info, b"")
    for child in sorted(root.iterdir()):
        member = f"{arcname}/{child.name}"
        if child.is_symlink():
            _add_symlink(archive, child, member, root=root)
        elif child.is_dir():
            _add_directory(archive, child, member)
        elif child.is_file():
            _add_file(archive, child, member)
        else:
            raise TransferError(
                f"cannot upload special file {child} (fifo/socket/device are not allowed)"
            )


def _add_symlink(archive: zipfile.ZipFile, path: Path, arcname: str, *, root: Path) -> None:
    _check_arcname(arcname)
    target = path.readlink()
    resolved = (path.parent / target).resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise TransferError(f"symlink {path} escapes the uploaded tree (points at {resolved})")
    info = zipfile.ZipInfo(arcname)
    info.external_attr = _external_attributes(stat.S_IFLNK | 0o777)
    archive.writestr(info, str(target).encode("utf-8"))


def extract_download_zip(
    archive: zipfile.ZipFile,
    local: Path,
    *,
    max_bytes: int | None,
) -> None:
    """Extract a ``Compress-Archive`` zip so ``local`` receives the remote path.

    The archive's top-level component (the remote basename) is replaced by
    ``local``: a single-file archive lands exactly at ``local``; a directory
    archive lands its children under ``local``.
    """
    total = 0
    members = archive.infolist()
    if not members:
        raise TransferError("the sandbox returned an empty archive")
    for member in members:
        rel = _relative_member_name(member.filename)
        if rel is None:
            continue
        target = local if rel == Path("") else local.joinpath(*rel.parts)
        mode = member.external_attr >> 16
        if member.file_size > _MAX_MEMBER_BYTES:
            raise TransferError(
                f"archive member too large: {member.filename} ({member.file_size} bytes)"
            )
        if stat.S_ISLNK(mode):
            _extract_symlink(member, target, local, archive)
            continue
        if not (member.is_dir() or stat.S_ISREG(mode) or mode == 0):
            raise TransferError(f"refusing special file from sandbox: {member.filename}")
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise TransferError(f"refusing setuid/setgid file from sandbox: {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        total = _check_total(member, total, max_bytes)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as handle:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
        target.chmod(stat.S_IMODE(mode or 0o644))


def _relative_member_name(name: str) -> Path | None:
    """Drop the archive's top-level component; return None for names outside."""
    pure = Path(name.replace("\\", "/"))
    parts = [part for part in pure.parts if part not in ("/", "")]
    if not parts:
        return None
    if ".." in parts:
        raise TransferError(f"archive member escapes the export root: {name!r}")
    return Path(*parts[1:])


def _check_total(member: zipfile.ZipInfo, total: int, max_bytes: int | None) -> int:
    new_total = total + member.file_size
    if max_bytes is not None and new_total > max_bytes:
        raise TransferError(
            f"download exceeds max_bytes={max_bytes} "
            f"(member {member.filename} is {member.file_size} bytes)"
        )
    return new_total


def _extract_symlink(
    member: zipfile.ZipInfo,
    target: Path,
    root: Path,
    archive: zipfile.ZipFile,
) -> None:
    with archive.open(member) as source:
        raw = source.read(1 << 16)
    link = Path(raw.decode("utf-8", errors="strict"))
    if link.is_absolute() or ".." in link.parts:
        raise TransferError(
            f"refusing symlink escaping the sandbox export: {member.filename} -> {link}"
        )
    resolved = (target.parent / link).resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise TransferError(
            f"refusing symlink escaping the sandbox export: {member.filename} -> {link}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink() or os.path.lexists(target):
        target.unlink()
    target.symlink_to(link)
