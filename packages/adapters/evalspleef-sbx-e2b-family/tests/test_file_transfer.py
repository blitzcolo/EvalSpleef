"""File transfer semantics of the E2B-family base class through the fake SDK."""

from __future__ import annotations

import os
import stat

import pytest

from evalspleef.spi.errors import TransferError

from .conftest import make_spec


async def test_binary_and_text_roundtrip(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        payload = bytes(range(256)) * 4 + b"\r\n\x00tail"
        source = tmp_path / "payload.bin"
        source.write_bytes(payload)
        await sandbox.upload(source, "/workspace/rt/payload.bin")
        returned = tmp_path / "returned.bin"
        await sandbox.download("/workspace/rt/payload.bin", returned)
        assert returned.read_bytes() == payload
    finally:
        await sandbox.destroy()


async def test_single_file_mode_bits_are_preserved(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        source = tmp_path / "tool.sh"
        source.write_bytes(b"#!/bin/sh\necho hi\n")
        os.chmod(source, 0o750)
        await sandbox.upload(source, "/workspace/modes/tool.sh")
        listed = await sandbox.exec(
            [
                "python3",
                "-c",
                "import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o7777))",
                "modes/tool.sh",
            ]
        )
        assert listed.exit_code == 0, listed.stderr
        assert listed.stdout.strip() == b"0o750"
    finally:
        await sandbox.destroy()


async def test_directory_roundtrip_with_permissions(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        tree = tmp_path / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "readme.txt").write_bytes(b"alpha\n")
        (tree / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02")
        os.chmod(tree / "readme.txt", 0o640)
        os.chmod(tree / "sub" / "nested.bin", 0o755)
        await sandbox.upload(tree, "/workspace/dir-roundtrip/tree")
        back = tmp_path / "back"
        await sandbox.download("/workspace/dir-roundtrip/tree", back)
        assert (back / "readme.txt").read_bytes() == b"alpha\n"
        assert (back / "sub" / "nested.bin").read_bytes() == b"\x00\x01\x02"
        assert stat.S_IMODE((back / "readme.txt").stat().st_mode) == 0o640
        assert stat.S_IMODE((back / "sub" / "nested.bin").stat().st_mode) == 0o755
    finally:
        await sandbox.destroy()


async def test_download_rejects_escaping_symlink(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        created = await sandbox.exec(["ln", "-s", "/etc/passwd", "escape"])
        assert created.exit_code == 0, created.stderr
        with pytest.raises(TransferError):
            await sandbox.download("/workspace/escape", tmp_path / "escape")
    finally:
        await sandbox.destroy()


async def test_download_enforces_max_bytes(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        source = tmp_path / "big.bin"
        source.write_bytes(b"x" * 4096)
        await sandbox.upload(source, "/workspace/big.bin")
        with pytest.raises(TransferError):
            await sandbox.download("/workspace/big.bin", tmp_path / "out", max_bytes=1024)
    finally:
        await sandbox.destroy()


async def test_upload_rejects_tree_with_escaping_symlink(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        tree = tmp_path / "badtree"
        tree.mkdir()
        (tree / "link").symlink_to("/etc/passwd")
        with pytest.raises(TransferError):
            await sandbox.upload(tree, "/workspace/badtree")
    finally:
        await sandbox.destroy()


async def test_upload_rejects_paths_outside_workspace(novita_provider, tmp_path) -> None:
    sandbox = await novita_provider.create(make_spec())
    try:
        source = tmp_path / "x.txt"
        source.write_bytes(b"x")
        with pytest.raises(TransferError):
            await sandbox.upload(source, "/etc/x.txt")
        with pytest.raises(TransferError):
            await sandbox.download("/etc/passwd", tmp_path / "y")
    finally:
        await sandbox.destroy()


async def test_selfhost_file_roundtrip(selfhost_provider, tmp_path) -> None:
    sandbox = await selfhost_provider.create(make_spec(kind="e2b_template"))
    try:
        payload = b"selfhost-\x00-payload"
        source = tmp_path / "p.bin"
        source.write_bytes(payload)
        await sandbox.upload(source, "/workspace/sh/p.bin")
        returned = tmp_path / "r.bin"
        await sandbox.download("/workspace/sh/p.bin", returned)
        assert returned.read_bytes() == payload
    finally:
        await sandbox.destroy()
