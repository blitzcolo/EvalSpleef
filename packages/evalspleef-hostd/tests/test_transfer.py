from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from evalspleef.hostd import transfer
from evalspleef.hostd._pb import hostd_pb2 as pb
from evalspleef.spi.errors import TransferError


def _chunks(
    path: Path, size: int = transfer.CHUNK_SIZE
) -> tuple[list[pb.TransferChunk], pb.TransferTrailer]:
    reader = transfer.ChunkReader(path, size)
    chunks = list(reader)
    return chunks, reader.trailer


def test_chunks_are_four_mib_and_hashed(tmp_path: Path) -> None:
    payload = bytes(range(256)) * (9 * 4096)  # 9 MiB
    source = tmp_path / "big.bin"
    source.write_bytes(payload)
    chunks, trailer = _chunks(source)
    assert [len(c.data) for c in chunks] == [4 << 20, 4 << 20, 1 << 20]
    assert all(c.sha256 == hashlib.sha256(c.data).hexdigest() for c in chunks)
    assert trailer.total_bytes == len(payload)
    assert trailer.sha256 == hashlib.sha256(payload).hexdigest()
    sink = io.BytesIO()
    receiver = transfer.ChunkReceiver(sink)
    for chunk in chunks:
        receiver.accept(chunk)
    assert receiver.finish(trailer) == len(payload)
    assert sink.getvalue() == payload


def test_corrupted_chunk_is_detected_on_arrival(tmp_path: Path) -> None:
    source = tmp_path / "f.bin"
    source.write_bytes(b"a" * 3000)
    chunks, _ = _chunks(source, 1000)
    corrupted = bytearray(chunks[1].data)
    corrupted[10] ^= 0xFF
    chunks[1].data = bytes(corrupted)
    receiver = transfer.ChunkReceiver(io.BytesIO())
    receiver.accept(chunks[0])
    with pytest.raises(TransferError, match="chunk 1"):
        receiver.accept(chunks[1])


def test_lost_or_reordered_chunks_are_detected(tmp_path: Path) -> None:
    source = tmp_path / "f.bin"
    source.write_bytes(b"b" * 3000)
    chunks, trailer = _chunks(source, 1000)
    skipped = transfer.ChunkReceiver(io.BytesIO())
    skipped.accept(chunks[0])
    with pytest.raises(TransferError, match="out of order"):
        skipped.accept(chunks[2])
    truncated = transfer.ChunkReceiver(io.BytesIO())
    truncated.accept(chunks[0])
    truncated.accept(chunks[1])
    with pytest.raises(TransferError, match="incomplete"):
        truncated.finish(trailer)


def test_whole_payload_hash_catches_a_forged_trailer(tmp_path: Path) -> None:
    source = tmp_path / "f.bin"
    source.write_bytes(b"c" * 2000)
    chunks, trailer = _chunks(source, 1000)
    receiver = transfer.ChunkReceiver(io.BytesIO())
    for chunk in chunks:
        receiver.accept(chunk)
    trailer.sha256 = "0" * 64
    with pytest.raises(TransferError, match="whole payload"):
        receiver.finish(trailer)


def test_pack_and_unpack_keep_modes_and_links(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    script = tree / "sub" / "run.sh"
    script.write_text("echo hi\n")
    script.chmod(0o750)
    (tree / "link").symlink_to("sub/run.sh")
    archive = tmp_path / "t.tar"
    transfer.pack(tree, archive)
    payload = transfer.unpack(archive, tmp_path / "out", strict=True)
    assert (payload / "sub" / "run.sh").stat().st_mode & 0o777 == 0o750
    assert (payload / "link").is_symlink()


def test_strict_unpack_refuses_links_leaving_the_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "escape").symlink_to("/etc/passwd")
    archive = tmp_path / "t.tar"
    transfer.pack(tree, archive)
    with pytest.raises(TransferError):
        transfer.unpack(archive, tmp_path / "out", strict=True)
