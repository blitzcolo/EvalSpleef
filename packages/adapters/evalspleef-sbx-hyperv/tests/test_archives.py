"""Zip (Windows guests) and tar (Linux guests) packing and sanitised extraction."""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from evalspleef_sbx_hyperv._tar import build_upload_tar, extract_download_tar
from evalspleef_sbx_hyperv._zip import build_upload_zip, extract_download_zip

from evalspleef.spi.errors import TransferError

REG = stat.S_IFREG


def _tree(root: Path) -> Path:
    tree = root / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "readme.txt").write_bytes(b"alpha\n")
    (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
    (tree / "readme.txt").chmod(0o640)
    (tree / "sub" / "nested.bin").chmod(0o755)
    return tree


def _zip(members: list[tuple[str, bytes, int]]) -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, mode in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def _tar(
    members: list[tarfile.TarInfo], payloads: dict[str, bytes] | None = None
) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info in members:
            data = (payloads or {}).get(info.name, b"")
            info.size = len(data) if info.isreg() else 0
            archive.addfile(info, io.BytesIO(data) if info.isreg() else None)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r:")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- zip ------------------------------------------------------------------------------


def test_zip_round_trip_keeps_content_and_permission_bits(tmp_path: Path) -> None:
    data = build_upload_zip(_tree(tmp_path), arcname="tree")
    out = tmp_path / "out"
    extract_download_zip(zipfile.ZipFile(io.BytesIO(data)), out, max_bytes=None)
    assert (out / "readme.txt").read_bytes() == b"alpha\n"
    assert (out / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
    assert _mode(out / "readme.txt") == 0o640
    assert _mode(out / "sub" / "nested.bin") == 0o755


def test_zip_keeps_an_in_tree_symlink(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "link").symlink_to("readme.txt")
    out = tmp_path / "out"
    extract_download_zip(
        zipfile.ZipFile(io.BytesIO(build_upload_zip(tree, arcname="tree"))), out, max_bytes=None
    )
    assert (out / "link").is_symlink()
    assert os.readlink(out / "link") == "readme.txt"


def test_zip_upload_rejects_a_symlink_that_leaves_the_tree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    (tree / "escape").symlink_to("../outside.txt")
    with pytest.raises(TransferError, match="escapes"):
        build_upload_zip(tree, arcname="tree")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_zip_upload_rejects_special_files(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    os.mkfifo(tree / "pipe")
    with pytest.raises(TransferError, match="special file"):
        build_upload_zip(tree, arcname="tree")


@pytest.mark.parametrize("arcname", ["", "/abs", "../up", "a/../../b"])
def test_zip_upload_rejects_unsafe_archive_names(tmp_path: Path, arcname: str) -> None:
    with pytest.raises(TransferError, match="unsafe"):
        build_upload_zip(_tree(tmp_path), arcname=arcname)


def test_zip_upload_rejects_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="does not exist"):
        build_upload_zip(tmp_path / "nope", arcname="nope")


@pytest.mark.parametrize(
    ("members", "fragment"),
    [
        ([("tree/../../evil", b"x", REG | 0o644)], "escapes"),
        ([("tree/link", b"/etc/passwd", stat.S_IFLNK | 0o777)], "symlink"),
        ([("tree/link", b"../../etc/passwd", stat.S_IFLNK | 0o777)], "symlink"),
        ([("tree/suid", b"x", REG | stat.S_ISUID | 0o755)], "setuid"),
        ([("tree/pipe", b"", stat.S_IFIFO | 0o644)], "special file"),
    ],
)
def test_zip_download_rejects_hostile_members(
    tmp_path: Path, members: list[tuple[str, bytes, int]], fragment: str
) -> None:
    with pytest.raises(TransferError, match=fragment):
        extract_download_zip(_zip(members), tmp_path / "out", max_bytes=None)


def test_zip_download_enforces_max_bytes_and_rejects_empty(tmp_path: Path) -> None:
    big = _zip([("tree/a.bin", b"0123456789", REG | 0o644)])
    with pytest.raises(TransferError, match="max_bytes"):
        extract_download_zip(big, tmp_path / "out", max_bytes=5)
    with pytest.raises(TransferError, match="empty"):
        extract_download_zip(_zip([]), tmp_path / "out2", max_bytes=None)


def test_zip_single_file_lands_exactly_at_the_target(tmp_path: Path) -> None:
    archive = _zip([("report.txt", b"body", REG | 0o644)])
    target = tmp_path / "copy.txt"
    extract_download_zip(archive, target, max_bytes=None)
    assert target.read_bytes() == b"body"


# -- tar ------------------------------------------------------------------------------


def test_tar_round_trip_keeps_content_and_permission_bits(tmp_path: Path) -> None:
    data = build_upload_tar(_tree(tmp_path), arcname="tree")
    out = tmp_path / "out"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        extract_download_tar(archive, out, max_bytes=None)
    assert (out / "readme.txt").read_bytes() == b"alpha\n"
    assert _mode(out / "readme.txt") == 0o640
    assert _mode(out / "sub" / "nested.bin") == 0o755


def test_tar_upload_rejects_a_symlink_that_leaves_the_tree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    (tree / "escape").symlink_to("../outside.txt")
    with pytest.raises(TransferError, match="escapes"):
        build_upload_tar(tree, arcname="tree")


def _member(name: str, kind: bytes, mode: int, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.linkname = linkname
    return info


@pytest.mark.parametrize(
    ("member", "fragment"),
    [
        (_member("tree/../../evil", tarfile.REGTYPE, 0o644), "escapes"),
        (_member("tree/link", tarfile.SYMTYPE, 0o777, "/etc/passwd"), "symlink"),
        (_member("tree/link", tarfile.SYMTYPE, 0o777, "../../etc/passwd"), "symlink"),
        (_member("tree/suid", tarfile.REGTYPE, 0o4755), "setuid"),
        (_member("tree/pipe", tarfile.FIFOTYPE, 0o644), "device or fifo"),
    ],
)
def test_tar_download_rejects_hostile_members(
    tmp_path: Path, member: tarfile.TarInfo, fragment: str
) -> None:
    with pytest.raises(TransferError, match=fragment):
        extract_download_tar(_tar([member]), tmp_path / "out", max_bytes=None)


def test_tar_download_enforces_max_bytes_and_rejects_empty(tmp_path: Path) -> None:
    member = _member("tree/a.bin", tarfile.REGTYPE, 0o644)
    archive = _tar([member], {"tree/a.bin": b"0123456789"})
    with pytest.raises(TransferError, match="max_bytes"):
        extract_download_tar(archive, tmp_path / "out", max_bytes=5)
    with pytest.raises(TransferError, match="empty"):
        extract_download_tar(_tar([]), tmp_path / "out2", max_bytes=None)
