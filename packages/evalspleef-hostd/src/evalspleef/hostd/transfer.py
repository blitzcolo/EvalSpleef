"""Chunked file transfer: 4 MiB chunks, each carrying a sha256 checked when it arrives.

Every transfer is a tar archive with one top-level member called ``payload`` (a file, a
directory or a symlink), so permission bits and links cross the wire unchanged. A chunk hash
detects corruption of one chunk; the trailer (count, size and whole-payload hash) detects lost,
duplicated or reordered chunks.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from evalspleef.spi.errors import TransferError

from ._pb import hostd_pb2 as pb

CHUNK_SIZE = 4 << 20
PAYLOAD_NAME = "payload"


class ChunkReader:
    """Iterate a file as hashed chunks; ``trailer`` is complete once iteration finished."""

    def __init__(self, path: Path, chunk_size: int = CHUNK_SIZE) -> None:
        self._path = path
        self._chunk_size = chunk_size
        self._total_hash = hashlib.sha256()
        self._count = 0
        self._bytes = 0
        self._finished = False

    def __iter__(self) -> Iterator[pb.TransferChunk]:
        with self._path.open("rb") as handle:
            while block := handle.read(self._chunk_size):
                self._total_hash.update(block)
                chunk = pb.TransferChunk(
                    index=self._count, data=block, sha256=hashlib.sha256(block).hexdigest()
                )
                self._count += 1
                self._bytes += len(block)
                yield chunk
        self._finished = True

    @property
    def trailer(self) -> pb.TransferTrailer:
        if not self._finished:
            raise RuntimeError("trailer requested before the payload was fully read")
        return pb.TransferTrailer(
            chunk_count=self._count,
            total_bytes=self._bytes,
            sha256=self._total_hash.hexdigest(),
        )


class ChunkReceiver:
    """Write arriving chunks to ``sink``, verifying each hash and the final trailer."""

    def __init__(self, sink: BinaryIO, *, max_bytes: int | None = None) -> None:
        self._sink = sink
        self._max_bytes = max_bytes
        self._total_hash = hashlib.sha256()
        self._count = 0
        self._bytes = 0

    def accept(self, chunk: pb.TransferChunk) -> None:
        if chunk.index != self._count:
            raise TransferError(f"chunk out of order: expected {self._count}, got {chunk.index}")
        if hashlib.sha256(chunk.data).hexdigest() != chunk.sha256:
            raise TransferError(f"sha256 mismatch in chunk {chunk.index}: payload corrupted")
        self._bytes += len(chunk.data)
        if self._max_bytes is not None and self._bytes > self._max_bytes:
            raise TransferError(f"transfer exceeds {self._max_bytes} bytes")
        self._total_hash.update(chunk.data)
        self._sink.write(chunk.data)
        self._count += 1

    def finish(self, trailer: pb.TransferTrailer) -> int:
        if trailer.chunk_count != self._count or trailer.total_bytes != self._bytes:
            raise TransferError(
                f"transfer incomplete: received {self._count} chunks / {self._bytes} bytes, "
                f"sender sent {trailer.chunk_count} / {trailer.total_bytes}"
            )
        if trailer.sha256 != self._total_hash.hexdigest():
            raise TransferError("sha256 mismatch over the whole payload")
        self._sink.flush()
        return self._bytes


def pack(source: Path, archive: Path) -> None:
    """Tar ``source`` (file, directory or symlink) as the single member ``payload``."""
    with tarfile.open(archive, "w") as tar:
        tar.add(source, arcname=PAYLOAD_NAME, recursive=True)


def unpack(archive: Path, destination: Path, *, strict: bool) -> Path:
    """Extract an archive into ``destination`` and return the path of its payload.

    ``strict`` applies the stdlib ``data`` filter (no links or paths leaving the tree, special
    bits dropped) for archives that come from a sandbox; the control plane's own uploads keep
    their modes and use the ``tar`` filter, which still refuses paths outside ``destination``.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r") as tar:
            tar.extractall(destination, filter="data" if strict else "tar")
    except (tarfile.TarError, OSError) as exc:
        raise TransferError(f"cannot extract transferred archive: {exc}") from exc
    payload = destination / PAYLOAD_NAME
    if not payload.exists() and not payload.is_symlink():
        raise TransferError("transferred archive has no payload member")
    return payload


def replace_path(source: Path, target: Path) -> None:
    """Move ``source`` onto ``target``, replacing whatever is there."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    shutil.move(os.fspath(source), os.fspath(target))
