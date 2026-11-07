#!/usr/bin/env python3
"""A fake ``tart`` CLI for tests and the conformance target.

It emulates clone, set, run, exec, stop, delete, list, ip and --version against
local directories under ``$FAKE_TART_ROOT`` and real local subprocesses, so the
adapter under test genuinely spawns processes and parses CLI output. It is not a
real Tart installation and proves nothing about a real Apple Silicon host.

Layout: ``$FAKE_TART_ROOT/golden/<image>`` is the golden image, each VM is a
copy at ``$FAKE_TART_ROOT/vms/<name>/`` with ``workspace/`` and ``home/agent/``.
``exec`` strips the adapter's sudo wrapper and runs the command on the host with
the VM workspace as cwd, translating guest ``/workspace`` paths to the VM
directory. ``run --vnc-experimental`` answers with an RFB banner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

ROOT = Path(os.environ.get("FAKE_TART_ROOT", ""))


def _fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _vms() -> Path:
    return ROOT / "vms"


def _vm_dir(name: str) -> Path:
    return _vms() / name


def _meta_path(name: str) -> Path:
    return _vm_dir(name) / "meta.json"


def _read_meta(name: str) -> dict[str, Any] | None:
    path = _meta_path(name)
    if not path.exists():
        return None
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        return None


def _write_meta(name: str, meta: dict[str, Any]) -> None:
    _meta_path(name).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")


def _ensure_golden(source: str) -> None:
    image = source.split("@", 1)[0].replace("/", "_").replace(":", "_")
    golden = ROOT / "golden" / image
    if golden.exists():
        return
    (golden / "workspace").mkdir(parents=True)
    (golden / "home" / "agent").mkdir(parents=True)


def cmd_clone(args: list[str]) -> int:
    if len(args) != 2:
        return _fail("usage: tart clone <source> <name>")
    source, name = args
    if _vm_dir(name).exists():
        return _fail(f"VM {name!r} already exists")
    _ensure_golden(source)
    image = source.split("@", 1)[0].replace("/", "_").replace(":", "_")
    shutil.copytree(ROOT / "golden" / image, _vm_dir(name))
    _write_meta(name, {"id": name, "source": source, "running": False, "network": {}})
    return 0


def cmd_set(args: list[str]) -> int:
    if not args:
        return _fail("usage: tart set <name> [--cpu N] [--memory MB]")
    name = args[0]
    meta = _read_meta(name)
    if meta is None:
        return _fail(f"VM {name!r} does not exist")
    rest = args[1:]
    index = 0
    while index < len(rest):
        flag = rest[index]
        value = rest[index + 1] if index + 1 < len(rest) else ""
        if flag == "--cpu":
            meta["cpu"] = int(value)
        elif flag == "--memory":
            meta["memory"] = int(value)
        else:
            return _fail(f"unknown set option {flag!r}")
        index += 2
    _write_meta(name, meta)
    return 0


def _parse_run(args: list[str]) -> tuple[str | None, dict[str, Any], bool]:
    vm: str | None = None
    options: dict[str, Any] = {"allow": [], "block": [], "softnet": False, "graphics": True}
    vnc = False
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            if "=" in token:
                flag, value = token.split("=", 1)
            else:
                flag = token
                value = None
            if flag in ("--net-softnet-allow", "--net-softnet-block"):
                if value is None:
                    value = args[index + 1] if index + 1 < len(args) else ""
                    index += 1
                if flag == "--net-softnet-allow":
                    options["allow"].append(value)
                else:
                    options["block"].append(value)
            elif flag == "--net-softnet":
                options["softnet"] = True
            elif flag == "--no-graphics":
                options["graphics"] = False
            elif flag == "--vnc-experimental":
                vnc = True
            else:
                return vm, options | {"unknown": flag}, vnc
        else:
            vm = token
        index += 1
    return vm, options, vnc


async def _serve_vnc(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stop: asyncio.Event
) -> None:
    writer.write(b"RFB 003.008\n")
    await writer.drain()
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()
    stop.set()


async def _vnc_main(vm_dir: Path) -> int:
    """Serve one RFB banner on 127.0.0.1 and exit.

    The listener runs in its own detached process so the endpoint still answers
    right after stop and delete: the conformance GUI case reads the endpoint
    after destroying the sandbox. A real Tart VNC server dies with the VM; this
    is a property of the fake and is documented as such.
    """
    stop = asyncio.Event()
    server = await asyncio.start_server(
        lambda reader, writer: _serve_vnc(reader, writer, stop), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    tmp = vm_dir / "vnc-port.tmp"
    tmp.write_text(str(port), encoding="utf-8")
    os.replace(tmp, vm_dir / "vnc-port")
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=120)
    server.close()
    await server.wait_closed()
    return 0


def _start_vnc_helper(vm_dir: Path) -> int | None:
    subprocess.Popen(
        [sys.executable, __file__, "_vnc-serve", str(vm_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    port_file = vm_dir / "vnc-port"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if port_file.exists():
            text = port_file.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.01)
    return None


async def _run_vm(name: str, meta: dict[str, Any], vnc: bool) -> int:
    if vnc:
        port = _start_vnc_helper(_vm_dir(name))
        if port is None:
            return _fail(f"VNC helper for {name!r} did not report a port")
        print(f"VNC server is running at vnc://127.0.0.1:{port}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    meta["running"] = False
    _write_meta(name, meta)
    return 0


def cmd_run(args: list[str]) -> int:
    vm, options, vnc = _parse_run(args)
    if vm is None:
        return _fail("usage: tart run [options] <name>")
    if "unknown" in options:
        return _fail(f"unknown run option {options['unknown']!r}")
    meta = _read_meta(vm)
    if meta is None:
        return _fail(f"VM {vm!r} does not exist")
    meta.update(
        running=True,
        pid=os.getpid(),
        network={"allow": options["allow"], "block": options["block"]},
        gui=not options["graphics"],
    )
    _write_meta(vm, meta)
    return asyncio.run(_run_vm(vm, meta, vnc))


def _strip_sudo(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "sudo":
        return argv
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in ("-u", "-g", "-p", "-U"):
            index += 2
            continue
        index += 1
    return argv[index:]


def cmd_exec(args: list[str]) -> int:
    if len(args) < 2:
        return _fail("usage: tart exec <name> -- <command...>")
    name = args[0]
    rest = args[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    meta = _read_meta(name)
    if meta is None or not meta.get("running"):
        return _fail(f"guest agent is not running for {name!r}", 125)
    if os.environ.get("FAKE_TART_GUEST_AGENT") == "down":
        return _fail(f"guest agent is not running for {name!r}", 125)
    command = _strip_sudo(list(rest))
    extra_env: dict[str, str] = {}
    if command and command[0] == "env":
        command = command[1:]
        while command and "=" in command[0]:
            key, _, value = command[0].partition("=")
            extra_env[key] = value
            command = command[1:]
    if not command:
        return _fail("empty command")
    workspace = _vm_dir(name) / "workspace"
    home = _vm_dir(name) / "home" / "agent"
    translated = [
        token.replace("/workspace", str(workspace)) if "/workspace" in token else token
        for token in command
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(_vm_dir(name) / "tmp"),
        **extra_env,
    }
    (Path(env["TMPDIR"])).mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            translated,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            cwd=str(workspace if workspace.exists() else _vm_dir(name)),
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return _fail(f"command not found: {translated[0]!r}", 127)
    return int(completed.returncode)


def cmd_stop(args: list[str]) -> int:
    if len(args) != 1:
        return _fail("usage: tart stop <name>")
    meta = _read_meta(args[0])
    if meta is None:
        return _fail(f"VM {args[0]!r} does not exist")
    pid = meta.get("pid")
    if pid:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = _read_meta(args[0])
        if current is None or not current.get("running"):
            return 0
        time.sleep(0.01)
    return 0


def cmd_delete(args: list[str]) -> int:
    if len(args) != 1:
        return _fail("usage: tart delete <name>")
    shutil.rmtree(_vm_dir(args[0]), ignore_errors=True)
    return 0


def cmd_list(args: list[str]) -> int:
    print("NAME")
    if _vms().exists():
        for path in sorted(_vms().iterdir()):
            if path.is_dir():
                print(path.name)
    return 0


def cmd_ip(args: list[str]) -> int:
    if len(args) != 1:
        return _fail("usage: tart ip <name>")
    if _read_meta(args[0]) is None:
        return _fail(f"VM {args[0]!r} does not exist")
    print("127.0.0.1")
    return 0


def main(argv: list[str]) -> int:
    if not str(ROOT) or not ROOT.exists():
        return _fail("FAKE_TART_ROOT is not set to an existing directory")
    if not argv:
        return _fail("usage: tart <command> ...")
    command, args = argv[0], argv[1:]
    if command in ("--version", "version"):
        print("tart 2.20.0 (spleef fake)")
        return 0
    if command == "_vnc-serve":
        return asyncio.run(_vnc_main(Path(args[0])))
    handlers = {
        "clone": cmd_clone,
        "set": cmd_set,
        "run": cmd_run,
        "exec": cmd_exec,
        "stop": cmd_stop,
        "delete": cmd_delete,
        "list": cmd_list,
        "ip": cmd_ip,
    }
    handler = handlers.get(command)
    if handler is None:
        return _fail(f"unknown command {command!r}")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
