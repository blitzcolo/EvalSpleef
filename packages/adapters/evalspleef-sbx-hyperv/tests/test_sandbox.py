"""Sandbox tests: exec over SSH, timeouts, file transfer and the PowerShell Direct fallback.

The guest is a local asyncssh server that runs command lines through ``/bin/sh``
and understands a small PowerShell-like subset. A Windows guest is *simulated*:
these tests prove the provider's SSH/SFTP plumbing and command construction, not
that real Windows PowerShell or OpenSSH for Windows behave the same way.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import shutil
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from evalspleef_sbx_hyperv._fake_host_shell import FakeHostShell
from evalspleef_sbx_hyperv.provider import HyperVProvider
from evalspleef_sbx_hyperv.sandbox import HyperVSandbox, _inspect_downloaded_tree

from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, SpleefError, TransferError
from evalspleef.spi.sandbox import SandboxSpec

SpecFactory = Callable[..., SandboxSpec]
ProviderFactory = Callable[..., HyperVProvider]


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, ref: str) -> str:
        return self._values[ref]


def _guest_path(shell: FakeHostShell, sandbox: HyperVSandbox, relative: str) -> Path:
    return shell.guest(sandbox.id).root / "workspace" / relative


def _write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _decode(remote: str) -> str:
    """The PowerShell script inside a ``-EncodedCommand`` command line."""
    return base64.b64decode(remote.rsplit(" ", 1)[1]).decode("utf-16-le")


def _local_tree(root: Path) -> Path:
    tree = root / "tree"
    _write(tree / "readme.txt", b"alpha\n", 0o640)
    _write(tree / "sub" / "nested.bin", b"\x00\x01\x02", 0o755)
    return tree


# -- exec, windows guest --------------------------------------------------------------


async def test_list_command_goes_out_as_a_quoted_command_line(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell
) -> None:
    failed = await sandbox.exec(["cmd", "/c", "exit 7"])
    assert failed.exit_code == 7
    assert 'cmd /c "exit 7"' in fake_shell.guest(sandbox.id).commands
    echoed = await sandbox.exec(["echo", "hello"])
    assert (echoed.exit_code, echoed.stdout) == (0, b"hello\n")


async def test_string_command_runs_as_encoded_powershell(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell
) -> None:
    script = "Write-Output -NoNewline 'out-line'; [Console]::Error.Write('err-line')"
    result = await sandbox.exec(script)
    assert (result.exit_code, result.stdout, result.stderr) == (0, b"out-line", b"err-line")
    remote = fake_shell.guest(sandbox.id).commands[-1]
    assert remote.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert _decode(remote) == script


async def test_native_command_exit_code_is_reported_verbatim(sandbox: HyperVSandbox) -> None:
    result = await sandbox.exec("& 'sh' '-c' 'exit 5'")
    assert result.exit_code == 5
    assert not result.timed_out


async def test_streaming_delivers_each_stream_in_order(sandbox: HyperVSandbox) -> None:
    events: list[tuple[str, bytes]] = []

    def on_output(stream: str, data: bytes) -> None:
        events.append((stream, data))

    result = await sandbox.exec(
        "Write-Output -NoNewline 'a1'; [Console]::Error.Write('b1'); Write-Output -NoNewline 'a2'",
        on_output=on_output,
    )
    streamed_out = b"".join(data for name, data in events if name == "stdout")
    streamed_err = b"".join(data for name, data in events if name == "stderr")
    assert (streamed_out, streamed_err) == (b"a1a2", b"b1")
    assert (result.stdout, result.stderr) == (streamed_out, streamed_err)
    assert not result.truncated


async def test_oversized_output_is_truncated_with_head_and_tail(sandbox: HyperVSandbox) -> None:
    result = await sandbox.exec("$s = 'x' * 65536; [Console]::Out.Write($s)", max_output_bytes=1024)
    assert result.exit_code == 0
    assert result.truncated
    assert 0 < len(result.stdout) <= 1024


async def test_env_and_cwd_are_applied_through_the_script(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell
) -> None:
    result = await sandbox.exec("[Console]::Out.Write($env:SPLEEF_T)", env={"SPLEEF_T": "it's"})
    assert result.stdout == b"it's"
    written = await sandbox.exec(["sh", "-c", "printf marker > m.txt"], cwd="/workspace/sub")
    assert written.exit_code == 0
    assert _read(_guest_path(fake_shell, sandbox, "sub/m.txt")) == b"marker"


async def test_stdin_reaches_the_command(sandbox: HyperVSandbox) -> None:
    result = await sandbox.exec(["sh", "-c", "cat"], stdin=b"piped-in")
    assert result.stdout == b"piped-in"


async def test_other_users_are_refused(sandbox: HyperVSandbox) -> None:
    with pytest.raises(CapabilityNotSupported):
        await sandbox.exec(["echo", "x"], user="root")
    assert (await sandbox.exec(["echo", "x"], user="agent")).exit_code == 0


async def test_concurrent_exec_calls_stay_separate(sandbox: HyperVSandbox) -> None:
    async def one(index: int) -> bytes:
        result = await sandbox.exec(
            f"Start-Sleep -Milliseconds 100; Write-Output -NoNewline 'task-{index}'"
        )
        return result.stdout

    outputs = await asyncio.gather(*(one(index) for index in range(4)))
    assert outputs == [f"task-{index}".encode() for index in range(4)]


async def test_windows_timeout_kills_the_tree_with_taskkill(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell
) -> None:
    guest = fake_shell.guest(sandbox.id)
    result = await sandbox.exec("Start-Sleep -Seconds 30", timeout_s=0.3)
    assert result.timed_out
    assert result.exit_code == 124
    assert result.duration_s < 10
    await guest.processes.wait_idle(5.0)
    reads = [c for c in guest.commands if c.startswith("cmd /c type C:/Windows/Temp/spleef-")]
    kills = [c for c in guest.commands if c.startswith("taskkill /T /F /PID ")]
    assert len(reads) == 1
    assert len(kills) == 1
    pidfile = PurePosixPath(reads[0].removeprefix("cmd /c type ")).name
    recorded = _read(guest.root / "C" / "Windows" / "Temp" / pidfile).decode().strip()
    assert recorded == kills[0].rsplit(" ", 1)[1]
    assert (await sandbox.exec(["echo", "still-here"])).exit_code == 0


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="the tree kill walks pgrep -P")
async def test_linux_timeout_kills_background_children(
    linux_sandbox: HyperVSandbox, fake_shell: FakeHostShell
) -> None:
    result = await linux_sandbox.exec("sleep 30 & echo $! > child.pid; wait", timeout_s=2.0)
    assert result.timed_out
    assert result.exit_code == 124
    child = _read(_guest_path(fake_shell, linux_sandbox, "child.pid")).decode().strip()
    assert child.isdigit()
    probe = await linux_sandbox.exec(
        f"i=0; while kill -0 {child} 2>/dev/null && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.05; done; "
        f"if kill -0 {child} 2>/dev/null; then echo alive; else echo gone; fi",
        timeout_s=20.0,
    )
    assert probe.stdout.strip() == b"gone"


async def test_linux_guest_exec_streams_and_takes_stdin(linux_sandbox: HyperVSandbox) -> None:
    result = await linux_sandbox.exec("printf 'out'; printf 'err' >&2; cat", stdin=b"-in")
    assert (result.exit_code, result.stdout, result.stderr) == (0, b"out-in", b"err")
    listed = await linux_sandbox.exec(["echo", "a b"], env={"SPLEEF_T": "v"})
    assert listed.stdout == b"a b\n"
    env_out = await linux_sandbox.exec('printf %s "$SPLEEF_T"', env={"SPLEEF_T": "it's"})
    assert env_out.stdout == b"it's"


# -- remote command construction --------------------------------------------------------


def test_remote_command_shapes(sandbox: HyperVSandbox, linux_sandbox: HyperVSandbox) -> None:
    def build(target: HyperVSandbox, cmd: Any, **kw: Any) -> tuple[str, str | None]:
        return target._remote_command(
            cmd, cwd=kw.get("cwd"), env=kw.get("env"), timeout_s=kw.get("timeout_s")
        )

    # Windows list commands use CommandLineToArgvW quoting and are not re-parsed.
    assert build(sandbox, ["cmd", "/c", "dir /b"]) == ('cmd /c "dir /b"', None)
    assert build(sandbox, ["mklink", "a", r"C:\Windows\win.ini"]) == (
        r"mklink a C:\Windows\win.ini",
        None,
    )
    assert build(sandbox, ["say", 'a "q" b'])[0] == r'say "a \"q\" b"'
    assert build(sandbox, ["dir", "C:\\my dir\\"])[0] == 'dir "C:\\my dir\\\\"'
    # Windows string commands become an encoded PowerShell script with cwd/env/pid prefixes.
    remote, pidfile = build(
        sandbox, "Get-Date", cwd="/workspace/x", env={"A": "b'c"}, timeout_s=5.0
    )
    assert pidfile is not None
    assert pidfile.startswith("C:/Windows/Temp/spleef-")
    assert _decode(remote) == (
        "$env:A = 'b''c'; Set-Location -LiteralPath '/workspace/x'; "
        f"$pid | Out-File -Encoding ascii -FilePath '{pidfile}'; Get-Date"
    )
    # Windows list commands that need cwd/env go through PowerShell with the call operator.
    wrapped, _ = build(sandbox, ["sh", "-c", "echo 'x'"], cwd="/workspace")
    assert _decode(wrapped) == "Set-Location -LiteralPath '/workspace'; & 'sh' '-c' 'echo ''x'''"

    # Linux: str -> sh -lc, list -> plain argv, extras compose with &&.
    assert build(linux_sandbox, "echo hi") == ("sh -lc 'echo hi'", None)
    assert build(linux_sandbox, ["echo", "a b"]) == ("echo 'a b'", None)
    remote, pidfile = build(linux_sandbox, "echo hi", cwd="/w", env={"A": "b"}, timeout_s=3.0)
    assert pidfile is not None
    assert pidfile.startswith("/tmp/spleef-")
    assert shlex.split(remote)[:2] == ["sh", "-c"]
    assert shlex.split(remote)[2] == (
        f"echo $$ > {pidfile} && cd /w && export A=b && exec sh -lc 'echo hi'"
    )


# -- file transfer, windows guest ---------------------------------------------------------


async def test_file_round_trip(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell, tmp_path: Path
) -> None:
    payload = bytes(range(256)) * 4 + b"\r\n\x00tail"
    source = tmp_path / "payload.bin"
    _write(source, payload)
    await sandbox.upload(source, "/workspace/data/payload.bin")
    assert _read(_guest_path(fake_shell, sandbox, "data/payload.bin")) == payload
    back = tmp_path / "back.bin"
    await sandbox.download("/workspace/data/payload.bin", back)
    assert _read(back) == payload


async def test_directory_round_trip_leaves_no_temp_archive(
    sandbox: HyperVSandbox, fake_shell: FakeHostShell, tmp_path: Path
) -> None:
    tree = _local_tree(tmp_path)
    await sandbox.upload(tree, "/workspace/proj/tree")
    assert _read(_guest_path(fake_shell, sandbox, "proj/tree/readme.txt")) == b"alpha\n"
    assert _read(_guest_path(fake_shell, sandbox, "proj/tree/sub/nested.bin")) == b"\x00\x01\x02"
    back = tmp_path / "back"
    await sandbox.download("/workspace/proj/tree", back)
    assert _read(back / "readme.txt") == b"alpha\n"
    assert _read(back / "sub" / "nested.bin") == b"\x00\x01\x02"
    leftovers = [p.name for p in _guest_path(fake_shell, sandbox, "proj").glob(".spleef-*")]
    assert leftovers == []


async def test_download_enforces_max_bytes_for_files_and_directories(
    sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    source = tmp_path / "ten.bin"
    _write(source, b"0123456789")
    await sandbox.upload(source, "/workspace/ten.bin")
    with pytest.raises(TransferError, match="max_bytes"):
        await sandbox.download("/workspace/ten.bin", tmp_path / "out.bin", max_bytes=5)
    await sandbox.upload(_local_tree(tmp_path), "/workspace/tree")
    with pytest.raises(TransferError, match="max_bytes"):
        await sandbox.download("/workspace/tree", tmp_path / "out", max_bytes=4)


async def test_transfer_refuses_bad_paths_and_missing_files(
    sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    source = tmp_path / "f.txt"
    _write(source, b"x")
    with pytest.raises(TransferError, match="absolute"):
        await sandbox.upload(source, "relative/f.txt")
    with pytest.raises(TransferError, match="escapes"):
        await sandbox.download("/workspace/../etc/passwd", tmp_path / "out")
    with pytest.raises(TransferError, match="stat"):
        await sandbox.download("/workspace/missing.txt", tmp_path / "out")


async def test_download_rejects_a_symlink_that_leaves_the_sandbox(
    sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    created = await sandbox.exec(["cmd", "/c", r"mklink escape C:\Windows\win.ini"])
    assert created.exit_code == 0
    with pytest.raises(TransferError, match="escaping"):
        await sandbox.download("/workspace/escape", tmp_path / "out")
    dotdot = await sandbox.exec(["cmd", "/c", "mklink up ../../outside"])
    assert dotdot.exit_code == 0
    with pytest.raises(TransferError, match="escaping"):
        await sandbox.download("/workspace/up", tmp_path / "out2")


async def test_download_follows_a_symlink_that_stays_inside(
    sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    source = tmp_path / "real.txt"
    _write(source, b"inside")
    await sandbox.upload(source, "/workspace/real.txt")
    assert (await sandbox.exec(["cmd", "/c", "mklink inner real.txt"])).exit_code == 0
    await sandbox.download("/workspace/inner", tmp_path / "copy.txt")
    assert _read(tmp_path / "copy.txt") == b"inside"


# -- file transfer, linux guest -------------------------------------------------------------


async def test_linux_directory_round_trip_keeps_permission_bits(
    linux_sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    await linux_sandbox.upload(_local_tree(tmp_path), "/workspace/proj/tree")
    back = tmp_path / "back"
    await linux_sandbox.download("/workspace/proj/tree", back)
    assert _read(back / "readme.txt") == b"alpha\n"
    assert _mode(back / "readme.txt") == 0o640
    assert _mode(back / "sub" / "nested.bin") == 0o755


async def test_linux_file_round_trip_and_size_cap(
    linux_sandbox: HyperVSandbox, tmp_path: Path
) -> None:
    source = tmp_path / "blob.bin"
    _write(source, b"\x00\xff" * 100)
    await linux_sandbox.upload(source, "/workspace/blob.bin")
    back = tmp_path / "back.bin"
    await linux_sandbox.download("/workspace/blob.bin", back)
    assert _read(back) == b"\x00\xff" * 100
    with pytest.raises(TransferError, match="max_bytes"):
        await linux_sandbox.download("/workspace/blob.bin", tmp_path / "x.bin", max_bytes=10)


# -- gui and lifecycle -----------------------------------------------------------------------


async def test_gui_endpoint_is_the_rdp_address(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> None:
    gui = await provider.create(spec_factory("gui-1", gui=True))
    plain = await provider.create(spec_factory("gui-2"))
    try:
        assert await gui.gui_endpoint() == "rdp://127.0.0.1:3390"
        with pytest.raises(CapabilityNotSupported):
            await plain.gui_endpoint()
    finally:
        await gui.destroy()
        await plain.destroy()


async def test_a_lost_vm_raises_sandbox_lost(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> None:
    sandbox = await provider.create(spec_factory("lost-1"))
    await provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    with pytest.raises(SandboxLost):
        await sandbox.upload(Path("."), "/workspace/x")
    await sandbox.destroy()


# -- PowerShell Direct fallback ------------------------------------------------------------------


async def _ps_direct_sandbox(
    provider_factory: ProviderFactory, spec_factory: SpecFactory
) -> tuple[HyperVProvider, HyperVSandbox]:
    provider = provider_factory(
        transfer="ps-direct",
        ps_direct_user="agent",
        ps_direct_password="env://GUEST_PASSWORD",
        secrets=_Secrets({"env://GUEST_PASSWORD": "s3cret"}),
    )
    sandbox = await provider.create(spec_factory("psd-1"))
    assert isinstance(sandbox, HyperVSandbox)
    return provider, sandbox


async def test_ps_direct_copies_files_and_trees_without_ssh(
    provider_factory: ProviderFactory,
    spec_factory: SpecFactory,
    fake_shell: FakeHostShell,
    tmp_path: Path,
) -> None:
    _, sandbox = await _ps_direct_sandbox(provider_factory, spec_factory)
    try:
        source = tmp_path / "in.txt"
        _write(source, b"through-vmbus")
        await sandbox.upload(source, "/workspace/data/in.txt")
        assert _read(_guest_path(fake_shell, sandbox, "data/in.txt")) == b"through-vmbus"
        (script,) = fake_shell.scripts("-ToSession")
        assert f"New-PSSession -VMName '{sandbox.id}'" in script
        assert "ConvertTo-SecureString 's3cret'" in script
        assert f"-Path '{source}' -Destination 'C:\\workspace\\data\\in.txt' -Recurse" in script

        _write(_guest_path(fake_shell, sandbox, "out/a.txt"), b"alpha")
        await sandbox.download("/workspace/out", tmp_path / "out")
        assert _read(tmp_path / "out" / "a.txt") == b"alpha"
        assert any("-FromSession" in c for c in fake_shell.commands)
        assert fake_shell.guest(sandbox.id).commands == []  # no SSH channel was opened
    finally:
        await sandbox.destroy()


async def test_ps_direct_needs_a_password_reference(
    provider_factory: ProviderFactory, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    provider = provider_factory(transfer="ps-direct")
    sandbox = await provider.create(spec_factory("psd-2"))
    try:
        source = tmp_path / "f.txt"
        _write(source, b"x")
        with pytest.raises(SpleefError, match="ps_direct_password"):
            await sandbox.upload(source, "/workspace/f.txt")
        with pytest.raises(TransferError, match="does not exist"):
            await sandbox.upload(tmp_path / "missing", "/workspace/m.txt")
    finally:
        await sandbox.destroy()


def test_ps_direct_download_inspection_flags_links_and_oversize(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    _write(clean / "a.txt", b"12345")
    assert _inspect_downloaded_tree(clean, None) is None
    assert _inspect_downloaded_tree(clean, 5) is None
    assert "max_bytes" in (_inspect_downloaded_tree(clean, 4) or "")
    linked = tmp_path / "linked"
    _write(linked / "a.txt", b"1")
    os.symlink("a.txt", linked / "alias")
    assert "symlink" in (_inspect_downloaded_tree(linked, None) or "")


async def test_exec_works_in_off_and_full_network_modes(
    provider: HyperVProvider, spec_factory: SpecFactory
) -> None:
    for mode in (NetMode.OFF, NetMode.FULL):
        sandbox = await provider.create(spec_factory(f"net-{mode.value}", network=mode))
        try:
            assert (await sandbox.exec(["echo", mode.value])).stdout == f"{mode.value}\n".encode()
        finally:
            await sandbox.destroy()
