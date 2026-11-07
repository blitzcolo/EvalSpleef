"""Fake libvirt host used by the conformance target and the package tests.

This module is a test double of the external system, not a real host: ``virsh``
and ``qemu-img`` are small Python executables placed in a temporary directory,
and the "guest" is a real local asyncssh server (key authentication) that runs
commands in a temporary directory. The SSH exec and SFTP paths therefore run for
real; the hypervisor does not. The conformance target built from this harness
reports ``fake=True``.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import asyncssh

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

from .provider import LibvirtProvider

_FAKE_VIRSH = r"""#!@PYTHON@
import base64
import json
import os
import sys

STATE = @STATE@
GUEST = @GUEST@


def load():
    try:
        with open(STATE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"domains": {}, "agent_pids": 0}


def save(state):
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def fail(message):
    sys.stderr.write(message + "\n")
    return 1


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "-c":
        args = args[2:]
    if not args:
        return fail("fake virsh: missing command")
    command = args[0]
    state = load()
    domains = state.setdefault("domains", {})
    if command == "version":
        print("Compiled against library: libvirt 10.0.0")
        print("Using library: libvirt 10.0.0")
        return 0
    if command == "list":
        for name in sorted(domains):
            print(name)
        return 0
    if command == "define":
        source = args[1] if len(args) > 1 else "-"
        xml = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
        if "<name>" not in xml:
            return fail("fake virsh: domain XML has no name")
        name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
        domains[name] = {"xml": xml, "state": "shut off"}
        save(state)
        workspace = os.path.join(GUEST, "workspaces", name)
        os.makedirs(workspace, exist_ok=True)
        link = os.path.join(GUEST, "workspace")
        try:
            if os.path.islink(link) or os.path.exists(link):
                os.unlink(link)
        except OSError:
            pass
        os.symlink(workspace, link)
        print("Domain '%s' defined from %s" % (name, source))
        return 0
    if command not in ("destroy", "undefine", "start", "domstate", "dumpxml"):
        if command.startswith("net-") or command.startswith("nwfilter-"):
            return 0
        if command == "domifaddr":
            return 1
        if command == "qemu-agent-command":
            return agent_command(args, state, domains)
        return fail("fake virsh: unsupported command " + command)
    if len(args) < 2 or args[1] not in domains:
        return fail("fake virsh: no domain " + (args[1] if len(args) > 1 else ""))
    name = args[1]
    if command == "destroy":
        domains[name]["state"] = "shut off"
        save(state)
        print("Domain '%s' destroyed" % name)
        return 0
    if command == "undefine":
        del domains[name]
        save(state)
        print("Domain '%s' has been undefined" % name)
        return 0
    if command == "start":
        domains[name]["state"] = "running"
        save(state)
        print("Domain '%s' started" % name)
        return 0
    if command == "domstate":
        print("running" if domains[name]["state"] == "running" else "shut off")
        return 0
    if command == "dumpxml":
        print(domains[name]["xml"])
        return 0
    return fail("fake virsh: unhandled command " + command)


def agent_command(args, state, domains):
    name = args[1] if len(args) > 1 else ""
    if name not in domains:
        return fail("fake virsh: no domain " + name)
    payload = json.loads(args[2])
    execute = payload.get("execute")
    if execute == "guest-exec":
        state["agent_pids"] = int(state.get("agent_pids", 0)) + 1
        save(state)
        print(json.dumps({"return": {"pid": state["agent_pids"]}}))
        return 0
    if execute == "guest-exec-status":
        out = base64.b64encode(b"guest-agent-fallback").decode()
        print(json.dumps({"return": {"exited": True, "exitcode": 0, "out-data": out}}))
        return 0
    return fail("fake virsh: unsupported agent command " + str(execute))


sys.exit(main())
"""

_FAKE_QEMU_IMG = r"""#!@PYTHON@
import json
import os
import sys


def fail(message):
    sys.stderr.write(message + "\n")
    return 1


def main():
    args = sys.argv[1:]
    if not args:
        return fail("fake qemu-img: missing subcommand")
    command = args[0]
    if command == "create":
        if "-b" not in args:
            return fail("fake qemu-img: create needs a backing file (-b)")
        backing = args[args.index("-b") + 1]
        target = args[-1]
        if not os.path.exists(backing):
            return fail("fake qemu-img: backing file does not exist: " + backing)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("qcow2 overlay backing=" + backing + "\n")
        print("Formatting '%s', fmt=qcow2 cluster_size=65536" % target)
        return 0
    if command == "info":
        path = args[-1]
        if not os.path.exists(path):
            return fail("fake qemu-img: no such file: " + path)
        info = {
            "filename": path,
            "format": "qcow2",
            "virtual-size": 10737418240,
        }
        with open(path, encoding="utf-8") as handle:
            content = handle.read().strip()
        if content.startswith("qcow2 overlay backing="):
            info["backing-filename"] = content.split("backing=", 1)[1]
        print(json.dumps(info))
        return 0
    return 0


sys.exit(main())
"""


class FakeLibvirtHost:
    """Temporary fake hypervisor plus a real local SSH guest."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="spleef-libvirt-fake-"))
        self.bin_dir = self.root / "bin"
        self.guest_root = self.root / "guest"
        self.state_file = self.root / "virsh-state.json"
        self.golden = self.root / "golden.qcow2"
        self.ssh_key = self.root / "id_ed25519"
        self.ssh_public_key = self.root / "id_ed25519.pub"
        self.ssh_port = 0
        self.rfb_port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ssh_server: asyncssh.SSHAcceptor | None = None
        self._rfb_server: asyncio.AbstractServer | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> FakeLibvirtHost:
        """Create the fake binaries and start the guest SSH and RFB servers."""
        for directory in (
            self.bin_dir,
            self.guest_root,
            self.guest_root / "home" / "agent",
            self.guest_root / "sys" / "fs" / "cgroup",
            self.guest_root / "workspaces",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (self.guest_root / "sys" / "fs" / "cgroup" / "cpu.max").write_text(
            "100000 100000\n", encoding="utf-8"
        )
        (self.guest_root / "sys" / "fs" / "cgroup" / "memory.max").write_text(
            str(512 * 1024 * 1024), encoding="utf-8"
        )
        self.golden.write_text("qcow2 golden image\n", encoding="utf-8")
        self._write_scripts()
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key.write_private_key(str(self.ssh_key))
        self.ssh_key.chmod(0o600)
        self.ssh_public_key.write_bytes(client_key.export_public_key())
        self._host_key = host_key
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("fake SSH guest did not start")
        if self._error is not None:
            raise RuntimeError(f"fake SSH guest failed to start: {self._error!r}")
        return self

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._close_servers(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> FakeLibvirtHost:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _write_scripts(self) -> None:
        replacements = {
            "@PYTHON@": sys.executable,
            "@STATE@": repr(str(self.state_file)),
            "@GUEST@": repr(str(self.guest_root)),
        }
        for name, template in (("virsh", _FAKE_VIRSH), ("qemu-img", _FAKE_QEMU_IMG)):
            script = template
            for token, value in replacements.items():
                script = script.replace(token, value)
            path = self.bin_dir / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        # The guest reports its hypervisor-assigned CPU count; the host may not have nproc.
        nproc = self.bin_dir / "nproc"
        nproc.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
        nproc.chmod(0o755)

    # -- servers ---------------------------------------------------------------

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._ssh_server = loop.run_until_complete(
                asyncssh.listen(
                    "127.0.0.1",
                    0,
                    server_host_keys=[self._host_key],
                    authorized_client_keys=[str(self.ssh_public_key)],
                    process_factory=self._handle_process,
                    sftp_factory=self._sftp_factory,
                    encoding=None,
                )
            )
            self._rfb_server = loop.run_until_complete(
                asyncio.start_server(self._handle_rfb, "127.0.0.1", 0)
            )
            self.ssh_port = self._ssh_server.get_port()
            sockets = self._rfb_server.sockets
            self.rfb_port = int(sockets[0].getsockname()[1]) if sockets else 0
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:  # pragma: no cover - startup failure path
            self._error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _close_servers(self) -> None:
        if self._ssh_server is not None:
            self._ssh_server.close()
            await self._ssh_server.wait_closed()
        if self._rfb_server is not None:
            self._rfb_server.close()
            await self._rfb_server.wait_closed()

    async def _handle_rfb(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"RFB 003.008\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    def _sftp_factory(self, channel: asyncssh.SSHServerChannel[Any]) -> asyncssh.SFTPServer:
        return asyncssh.SFTPServer(channel, chroot=str(self.guest_root).encode())

    async def _handle_process(self, process: asyncssh.SSHServerProcess[Any]) -> None:
        command = process.command
        try:
            text = command.decode("utf-8") if isinstance(command, bytes) else str(command)
            mapped = self._translate(text)
            environment = {
                "PATH": f"{self.bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(self.guest_root / "home" / "agent"),
                "LANG": "C.UTF-8",
                "TZ": "UTC",
                "SHELL": "/bin/sh",
            }
            child = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                mapped,
                cwd=str(self.guest_root),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException as exc:  # pragma: no cover - defensive
            process.stderr.write(f"fake guest error: {exc}\n")
            process.exit(1)
            return

        async def copy_stdout() -> None:
            assert child.stdout is not None
            while True:
                data = await child.stdout.read(65536)
                if not data:
                    return
                process.stdout.write(data)
                await process.stdout.drain()

        async def copy_stderr() -> None:
            assert child.stderr is not None
            while True:
                data = await child.stderr.read(65536)
                if not data:
                    return
                process.stderr.write(data)
                await process.stderr.drain()

        async def copy_stdin() -> None:
            assert child.stdin is not None
            while True:
                data = await process.stdin.read(65536)
                if not data:
                    break
                child.stdin.write(data)
                await child.stdin.drain()
            child.stdin.close()

        stdin_task = asyncio.ensure_future(copy_stdin())
        await asyncio.gather(copy_stdout(), copy_stderr())
        code = await child.wait()
        stdin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stdin_task
        process.exit(code)

    def _translate(self, command: str) -> str:
        """Map guest paths the way a real guest would see them."""
        translated = command.replace(
            "/sys/fs/cgroup", str(self.guest_root / "sys" / "fs" / "cgroup")
        )
        translated = translated.replace("/workspace", str(self.guest_root / "workspace"))
        return translated

    # -- provider and specs ----------------------------------------------------

    def provider(
        self,
        instance: str = "libvirt-fake",
        *,
        config: Mapping[str, Any] | None = None,
        use_path: bool = False,
    ) -> LibvirtProvider:
        base: dict[str, Any] = {
            "uri": "qemu:///session",
            "virsh_bin": str(self.bin_dir / "virsh"),
            "qemu_img_bin": str(self.bin_dir / "qemu-img"),
            "overlay_dir": str(self.root / "overlays"),
            "network_off": "spleef-off",
            "network_full": "default",
            "guest_os": ["linux", "windows"],
            "arches": ["x86_64"],
            "gui": "vnc",
            "ssh_host": "127.0.0.1",
            "ssh_port": self.ssh_port,
            "ssh_user": "agent",
            "ssh_key_file": str(self.ssh_key),
            "ssh_connect_timeout_s": 2.0,
            "startup_timeout_s": 15.0,
            "vnc_port": self.rfb_port,
            "max_concurrency": 4,
            "max_lifetime_s": 7200,
            "guest_agent_fallback": True,
        }
        if config:
            base.update(config)
        if use_path:
            base.pop("virsh_bin", None)
            base.pop("qemu_img_bin", None)
        return LibvirtProvider(instance, base)

    def spec_factory(
        self,
        trial_id: str,
        *,
        network: NetMode,
        max_lifetime_s: int,
    ) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.X86_64,
            template=TemplateRef(
                kind="qcow2",
                ref=str(self.golden),
                digest="sha256:" + "0" * 64,
            ),
            resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
            labels={
                "batch": "libvirt-fake",
                "trial": trial_id,
                "lease": f"lease-{trial_id}",
                "provider": "libvirt-fake",
            },
        )
