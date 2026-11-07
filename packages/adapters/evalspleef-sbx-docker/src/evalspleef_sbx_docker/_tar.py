"""Tar stream construction and sanitised extraction for upload/download.

Upload packs a local file or directory into the tar stream Docker's
``put_archive`` expects; download walks the ``get_archive`` tar and applies the
artifact sanitisation of PRD appendix D.2 I11: symlinks may not escape the
export root, special files are rejected, sizes are capped.
"""

from __future__ import annotations

import io
import stat
import tarfile
from pathlib import Path

from evalspleef.spi.errors import TransferError

_MAX_MEMBER_BYTES = 1 << 30
"""Refuse a single tar member larger than 1 GiB before streaming it to disk."""


def build_upload_tar(local: Path, *, arcname: str, contents_only: bool = False) -> bytes:
    """Pack ``local`` into a tar archive whose top-level entry is ``arcname``.

    Permission bits and mtimes are preserved; symlinks inside a uploaded tree are
    kept only when they stay inside the tree, otherwise the upload is rejected.
    With ``contents_only`` a directory is packed without its own entry, so extracting
    the archive never rewrites the metadata of the destination directory (a mount
    point such as /workspace is owned by root and cannot be changed by the agent).
    """
    if not local.exists() and not local.is_symlink():
        raise TransferError(f"local path does not exist: {local}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        if local.is_symlink():
            _add_symlink(tar, local, arcname, root=local.parent)
        elif local.is_dir():
            if contents_only:
                _add_children(tar, local, prefix="")
            else:
                _add_directory(tar, local, arcname)
        else:
            _add_file(tar, local, arcname)
    return buffer.getvalue()


def _check_arcname(arcname: str) -> None:
    if not arcname or arcname.startswith("/") or ".." in Path(arcname).parts:
        raise TransferError(f"unsafe archive member name: {arcname!r}")


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    _check_arcname(arcname)
    info = tar.gettarinfo(str(path), arcname=arcname)
    if not info.isreg():
        raise TransferError(f"only regular files can be uploaded: {path}")
    with path.open("rb") as handle:
        tar.addfile(info, handle)


def _add_directory(tar: tarfile.TarFile, root: Path, arcname: str) -> None:
    _check_arcname(arcname)
    info = tar.gettarinfo(str(root), arcname=arcname)
    if not info.isdir():
        raise TransferError(f"not a directory: {root}")
    tar.addfile(info)
    _add_children(tar, root, prefix=f"{arcname}/")


def _add_children(tar: tarfile.TarFile, root: Path, *, prefix: str) -> None:
    for child in sorted(root.iterdir()):
        member = f"{prefix}{child.name}"
        if child.is_symlink():
            _add_symlink(tar, child, member, root=root)
        elif child.is_dir():
            _add_directory(tar, child, member)
        elif child.is_file():
            _add_file(tar, child, member)
        else:
            raise TransferError(
                f"cannot upload special file {child} (fifo/socket/device are not allowed)"
            )


def _add_symlink(tar: tarfile.TarFile, path: Path, arcname: str, *, root: Path) -> None:
    _check_arcname(arcname)
    target = path.readlink()
    resolved = (path.parent / target).resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise TransferError(f"symlink {path} escapes the uploaded tree (points at {resolved})")
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.SYMTYPE
    info.linkname = str(target)
    info.mode = 0o777
    tar.addfile(info)


def extract_download_tar(
    archive: tarfile.TarFile,
    local: Path,
    *,
    max_bytes: int | None,
) -> None:
    """Extract a ``get_archive`` tar so that ``local`` receives the remote path.

    The archive's top-level component (the remote basename) is replaced by
    ``local``: a single-file archive lands exactly at ``local``; a directory
    archive lands its children under ``local``.
    """
    total = 0
    members = archive.getmembers()
    if not members:
        raise TransferError("the sandbox returned an empty archive")
    for member in members:
        rel = _relative_member_name(member.name)
        if rel is None:
            continue
        target = local if rel == Path("") else local.joinpath(*rel.parts)
        _check_member(member)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if member.issym():
            _extract_symlink(member, target, local)
            continue
        if not member.isreg():
            raise TransferError(f"refusing special file from sandbox: {member.name}")
        total += _check_size(member, total, max_bytes)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise TransferError(f"unreadable archive member: {member.name}")
        with source, target.open("wb") as handle:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
        target.chmod(stat.S_IMODE(member.mode))


def _relative_member_name(name: str) -> Path | None:
    """Drop the archive's top-level component; return None for names outside."""
    pure = Path(name)
    parts = [part for part in pure.parts if part not in ("/", "")]
    if not parts:
        return None
    if ".." in parts:
        raise TransferError(f"archive member escapes the export root: {name!r}")
    return Path(*parts[1:])


def _check_member(member: tarfile.TarInfo) -> None:
    if member.isdev() or member.isfifo():
        raise TransferError(f"refusing device or fifo from sandbox: {member.name}")
    if member.mode & (stat.S_ISUID | stat.S_ISGID):
        raise TransferError(f"refusing setuid/setgid file from sandbox: {member.name}")


def _check_size(member: tarfile.TarInfo, total: int, max_bytes: int | None) -> int:
    if member.size > _MAX_MEMBER_BYTES:
        raise TransferError(f"archive member too large: {member.name} ({member.size} bytes)")
    new_total = total + member.size
    if max_bytes is not None and new_total > max_bytes:
        raise TransferError(
            f"download exceeds max_bytes={max_bytes} (member {member.name} is {member.size} bytes)"
        )
    return member.size


def _extract_symlink(member: tarfile.TarInfo, target: Path, root: Path) -> None:
    link = Path(member.linkname)
    if link.is_absolute() or ".." in link.parts:
        raise TransferError(
            f"refusing symlink escaping the sandbox export: {member.name} -> {member.linkname}"
        )
    resolved = (target.parent / link).resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise TransferError(
            f"refusing symlink escaping the sandbox export: {member.name} -> {member.linkname}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(link)
