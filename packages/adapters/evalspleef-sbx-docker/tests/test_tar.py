"""Unit tests for the tar transfer helpers (no daemon needed)."""

from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest
from evalspleef_sbx_docker._tar import build_upload_tar, extract_download_tar

from evalspleef.spi.errors import TransferError


def _read_back(data: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:")


def test_upload_single_file_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    payload = bytes(range(256)) * 4
    source.write_bytes(payload)
    archive = _read_back(build_upload_tar(source, arcname="payload.bin"))
    members = archive.getmembers()
    assert [m.name for m in members] == ["payload.bin"]
    assert archive.extractfile(members[0]).read() == payload  # type: ignore[union-attr]
    assert stat.S_IMODE(members[0].mode) == 0o644


def test_upload_directory_preserves_permissions(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "readme.txt").write_bytes(b"alpha\n")
    (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
    (tree / "readme.txt").chmod(0o640)
    (tree / "sub" / "nested.bin").chmod(0o755)
    archive = _read_back(build_upload_tar(tree, arcname="tree"))
    modes = {m.name: stat.S_IMODE(m.mode) for m in archive.getmembers()}
    assert modes["tree/readme.txt"] == 0o640
    assert modes["tree/sub/nested.bin"] == 0o755


def test_upload_rejects_escaping_symlink(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to("/etc/passwd")
    with pytest.raises(TransferError, match="escapes"):
        build_upload_tar(tree, arcname="tree")


def test_upload_rejects_special_file(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        os.mkfifo(tree / "fifo")
    except PermissionError:  # pragma: no cover - platform without mkfifo
        pytest.skip("cannot create a fifo here")
    with pytest.raises(TransferError, match="special file"):
        build_upload_tar(tree, arcname="tree")


def _download_tar(entries: dict[str, tuple[bytes, int]]) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, (payload, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.mode = mode
            if payload:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r:")


def test_download_single_file(tmp_path: Path) -> None:
    archive = _download_tar({"payload.bin": (b"hello", 0o640)})
    target = tmp_path / "out.bin"
    extract_download_tar(archive, target, max_bytes=None)
    assert target.read_bytes() == b"hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_download_directory_tree(tmp_path: Path) -> None:
    archive = _download_tar(
        {
            "tree": (b"", 0o755),
            "tree/a.txt": (b"a", 0o644),
            "tree/sub/b.txt": (b"b", 0o600),
        }
    )
    target = tmp_path / "back"
    extract_download_tar(archive, target, max_bytes=None)
    assert (target / "a.txt").read_bytes() == b"a"
    assert (target / "sub" / "b.txt").read_bytes() == b"b"
    assert stat.S_IMODE((target / "sub" / "b.txt").stat().st_mode) == 0o600


def test_download_rejects_absolute_symlink(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    buffer.seek(0)
    with (
        tarfile.open(fileobj=buffer, mode="r:") as archive,
        pytest.raises(TransferError, match="symlink"),
    ):
        extract_download_tar(archive, tmp_path / "out", max_bytes=None)


def test_download_rejects_parent_escape_member(tmp_path: Path) -> None:
    archive = _download_tar({"../evil": (b"x", 0o644)})
    with pytest.raises(TransferError, match="escapes"):
        extract_download_tar(archive, tmp_path / "out", max_bytes=None)


def test_download_rejects_oversize(tmp_path: Path) -> None:
    archive = _download_tar({"big.bin": (b"x" * 1024, 0o644)})
    with pytest.raises(TransferError, match="max_bytes"):
        extract_download_tar(archive, tmp_path / "out", max_bytes=512)


def test_download_rejects_fifo(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("fifo")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)
    buffer.seek(0)
    with (
        tarfile.open(fileobj=buffer, mode="r:") as archive,
        pytest.raises(TransferError, match="fifo"),
    ):
        extract_download_tar(archive, tmp_path / "out", max_bytes=None)
