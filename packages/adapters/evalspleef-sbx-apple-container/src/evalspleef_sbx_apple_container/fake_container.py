#!/usr/bin/env python3
"""Stand-in for the apple/container CLI, used only by tests and conformance runs.

This script is deliberately a plain stdlib program: the adapter tests copy it to
a temporary directory, put it on ``PATH`` as ``container`` and let the provider
drive it. Each "container" is a directory tree under
``$SPLEEF_FAKE_CONTAINER_ROOT/containers/<name>``; ``exec`` runs the command on
the host inside that tree with a scrubbed environment, and a small wrapper
directory emulates the guest facts the isolation probes look for.

It is a fake of the external system, not an implementation of apple/container.
Where behaviour matters to the adapter it mirrors what ``container`` 1.6.1 does
(checked on macOS 27.2):

- ``--network none`` leaves only loopback (modelled with a blackhole proxy); an
  ``--internal`` network is host-only, so the host stays reachable from it;
- a process started by ``exec`` keeps running in the guest when the CLI process
  is killed (it runs in its own session here);
- ``cp`` reads and writes the root filesystem underneath ``--tmpfs`` mounts, so
  files it copies are invisible to ``exec`` under those mounts and vice versa;
- with ``--read-only`` only the ``--tmpfs`` mounts are writable (``mkdir`` and
  ``tar -x`` elsewhere fail with "Read-only file system"), and a tmpfs mount
  point belongs to root: ``tar -x`` of an archive whose entry is the mount point
  itself fails like GNU tar as the sandbox user ("Cannot utime", exit 2);
- JSON shapes of ``system version``, ``system status``, ``system property
  list``, ``list`` and ``inspect``, the ``--version`` line and the error text of
  a missing container or a taken name follow the real CLI; the app root holds a
  ``kernels/default.kernel-arm64`` link to a kernel image carrying a
  ``Linux version`` banner, like the real one.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import cast

ROOT_ENV = "SPLEEF_FAKE_CONTAINER_ROOT"
DEV_PROXY = "http://127.0.0.1:9"
_MAPPED_PREFIXES = ("/sys", "/workspace", "/home", "/tmp", "/proc/net")
_ARGV_PREFIXES = ("/workspace", "/home", "/tmp")
"""Guest paths passed as whole exec arguments are resolved inside the fake guest."""
VERSION = "1.6.1-fake"
KERNEL_RELEASE = "6.1.0-fake"
KERNEL_IMAGE = "vmlinux-6.1.0-7-fake"
VMINIT_IMAGE = "ghcr.io/apple/containerization/vminit:fake"


def state_root() -> Path:
    value = os.environ.get(ROOT_ENV)
    if not value:
        raise SystemExit(f"{ROOT_ENV} is not set")
    return Path(value)


def containers_root() -> Path:
    path = state_root() / "containers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def meta_path(name: str) -> Path:
    return containers_root() / name / "meta.json"


def load_meta(name: str) -> dict[str, object] | None:
    path = meta_path(name)
    if not path.exists():
        return None
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def save_meta(name: str, meta: dict[str, object]) -> None:
    path = meta_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def fs_root(name: str) -> Path:
    return containers_root() / name / "fs"


def lower_root(name: str) -> Path:
    """The root filesystem underneath the tmpfs mounts: what ``cp`` sees there."""
    return containers_root() / name / "lower"


def image_object(image: str) -> dict[str, object]:
    digest = image.partition("@")[2] or "sha256:" + "0" * 64
    return {"reference": image, "descriptor": {"digest": digest}}


def parse_memory(value: str) -> int:
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}
    suffix = value[-1:].upper()
    if suffix in units:
        return int(float(value[:-1]) * units[suffix])
    return int(value)


def ensure_wrapper_bin(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    tools = ("cat", "mkdir", "tar", "findmnt", "mount", "id", "whoami", "sudo", "nproc", "hostname")
    for tool in tools:
        path = bin_dir / tool
        content = (
            "#!/bin/sh\n"
            f'exec {shlex_quote(sys.executable)} {shlex_quote(script)} __wrap__ {tool} "$@"\n'
        )
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
    return bin_dir


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def cmd_run(args: list[str], *, start: bool = True) -> int:
    name: str | None = None
    tmpfs: list[str] = []
    read_only = False
    labels: dict[str, str] = {}
    env: dict[str, str] = {}
    user = "root"
    workdir = "/"
    cpus = 1.0
    memory_mb = 1024
    network: str | None = None
    detach = False
    remove = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-d", "--detach"):
            detach = True
            index += 1
        elif arg in ("-l", "--label"):
            key, _, value = args[index + 1].partition("=")
            labels[key] = value
            index += 2
        elif arg in ("-e", "--env"):
            key, _, value = args[index + 1].partition("=")
            env[key] = value
            index += 2
        elif arg in ("-u", "--user"):
            user = args[index + 1]
            index += 2
        elif arg in ("-w", "--workdir", "--cwd"):
            workdir = args[index + 1]
            index += 2
        elif arg in ("-c", "--cpus"):
            cpus = float(args[index + 1])
            index += 2
        elif arg in ("-m", "--memory"):
            memory_mb = parse_memory(args[index + 1]) // (1 << 20)
            index += 2
        elif arg == "--name":
            name = args[index + 1]
            index += 2
        elif arg == "--network":
            network = args[index + 1].split(",", 1)[0]
            index += 2
        elif arg == "--tmpfs":
            tmpfs.append(args[index + 1].split(":", 1)[0])
            index += 2
        elif arg in (
            "--cap-drop",
            "--cap-add",
            "--ulimit",
            "--shm-size",
            "--entrypoint",
        ):
            index += 2
        elif arg == "--rm" or arg == "--remove":
            remove = True
            index += 1
        elif arg == "--read-only":
            read_only = True
            index += 1
        elif arg in ("--init", "-i", "--interactive", "-t", "--tty"):
            index += 1
        elif arg.startswith("-"):
            print(f"container: unsupported option {arg}", file=sys.stderr)
            return 2
        else:
            break
    positional = args[index:]
    if not positional:
        print("container: run needs an image", file=sys.stderr)
        return 2
    image = positional[0]
    command = positional[1:]
    if not name:
        name = f"c-{os.urandom(8).hex()}"
    if load_meta(name) is not None:
        print(f"Error: container with id {name} already exists", file=sys.stderr)
        return 1
    root = fs_root(name)
    for directory in (
        root / "workspace",
        root / "tmp",
        root / "home" / user,
        root / "sys" / "fs" / "cgroup",
        root / "proc" / "net",
        *(root / mount.lstrip("/") for mount in tmpfs),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "sys" / "fs" / "cgroup" / "cpu.max").write_text(
        f"{int(cpus * 100000)} 100000\n", encoding="utf-8"
    )
    (root / "sys" / "fs" / "cgroup" / "memory.max").write_text(
        f"{memory_mb * 1024 * 1024}\n", encoding="utf-8"
    )
    (root / "sys" / "fs" / "cgroup" / "pids.max").write_text("max\n", encoding="utf-8")
    route_header = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    )
    route = route_header
    if network != "none":
        # The default NAT network and an internal (host-only) network both route
        # through a gateway on the host.
        route += "eth0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    (root / "proc" / "net" / "route").write_text(route, encoding="utf-8")
    ensure_wrapper_bin(state_root())
    meta: dict[str, object] = {
        "id": name,
        "image": image,
        "labels": labels,
        "env": env,
        "user": user,
        "workdir": workdir,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "network": network,
        "created_at": time.time(),
        "state": "running" if start else "stopped",
        "command": command,
        "detach": detach,
        "tmpfs": tmpfs,
        "read_only": read_only,
    }
    save_meta(name, meta)
    if not start:
        print(name)
        return 0
    if not detach:
        status = run_in_container(name, command, {}, workdir, interactive=False)
        if remove:
            shutil.rmtree(containers_root() / name, ignore_errors=True)
        return status
    print(name)
    return 0


def exec_environment(
    meta: dict[str, object], exec_env: dict[str, str], workdir: str
) -> dict[str, str]:
    user = str(meta.get("user") or "agent")
    name = str(meta.get("id"))
    root = fs_root(name)
    env = {
        "PATH": str(state_root() / "bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "home" / user),
        "PWD": workdir,
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "SPLEEF_FAKE_CONTAINER_ROOT": str(state_root()),
        "SPLEEF_FAKE_CONTAINER_ID": name,
        # Host quirk of the fake only: keep macOS tar from adding AppleDouble files.
        "COPYFILE_DISABLE": "1",
    }
    stored_env = meta.get("env")
    if isinstance(stored_env, dict):
        env.update({str(k): str(v) for k, v in stored_env.items()})
    env.update(exec_env)
    if meta.get("network") == "none" and "http_proxy" not in env:
        env["http_proxy"] = DEV_PROXY
        env["https_proxy"] = DEV_PROXY
        env["all_proxy"] = DEV_PROXY
        env["HTTP_PROXY"] = DEV_PROXY
        env["HTTPS_PROXY"] = DEV_PROXY
        env["ALL_PROXY"] = DEV_PROXY
    return env


def container_cwd(meta: dict[str, object], workdir: str) -> Path:
    name = str(meta.get("id"))
    root = fs_root(name)
    if workdir in ("", "/"):
        return root
    return root / workdir.lstrip("/")


def run_in_container(
    name: str,
    command: list[str],
    exec_env: dict[str, str],
    workdir: str,
    *,
    interactive: bool,
) -> int:
    meta = load_meta(name)
    if meta is None:
        print(f"Error: get failed: container {name} not found", file=sys.stderr)
        return 1
    if not command:
        print("container: exec needs a command", file=sys.stderr)
        return 2
    env = exec_environment(meta, exec_env, workdir)
    cwd = container_cwd(meta, workdir)
    cwd.mkdir(parents=True, exist_ok=True)
    root = fs_root(name)
    prefixes = tuple(tmpfs_mounts(meta)) or _ARGV_PREFIXES
    command = [mapped(arg, root, prefixes) for arg in command]
    # The provider follows the SPI and asks for ``sh -lc``. A login shell on this
    # host would reload /etc/profile and drop the fake wrapper directory from
    # PATH, so the fake runs the same script with ``-c`` wherever it appears.
    for position in range(len(command) - 1):
        if command[position] in ("sh", "/bin/sh") and command[position + 1] == "-lc":
            command[position + 1] = "-c"
    # Like the real guest, the process outlives a killed CLI: own session, own group.
    if interactive:
        payload = sys.stdin.buffer.read()
        completed = subprocess.run(
            command, cwd=cwd, env=env, input=payload, check=False, start_new_session=True
        )
    else:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
            start_new_session=True,
        )
    return completed.returncode


def cmd_exec(args: list[str]) -> int:
    index = 0
    exec_env: dict[str, str] = {}
    workdir: str | None = None
    interactive = False
    while index < len(args) and args[index].startswith("-"):
        arg = args[index]
        if arg in ("-e", "--env"):
            key, _, value = args[index + 1].partition("=")
            exec_env[key] = value
            index += 2
        elif arg in ("-w", "--workdir", "--cwd"):
            workdir = args[index + 1]
            index += 2
        elif arg in ("-u", "--user", "--uid", "--gid"):
            index += 2
        elif arg in ("-i", "--interactive", "-t", "--tty", "-d", "--detach"):
            interactive = interactive or arg in ("-i", "--interactive")
            index += 1
        else:
            print(f"container: unsupported exec option {arg}", file=sys.stderr)
            return 2
    if not args[index:]:
        print("container: exec needs a container id", file=sys.stderr)
        return 2
    name = args[index]
    command = args[index + 1 :]
    meta = load_meta(name)
    if meta is None:
        print(f"Error: get failed: container {name} not found", file=sys.stderr)
        return 1
    if meta.get("state") != "running":
        print(f"Error: container {name} is not running", file=sys.stderr)
        return 1
    effective_workdir = workdir or str(meta.get("workdir") or "/")
    return run_in_container(name, command, exec_env, effective_workdir, interactive=interactive)


def parse_ref(value: str) -> tuple[str | None, str]:
    prefix, separator, rest = value.partition(":")
    if separator and load_meta(prefix) is not None:
        return prefix, rest
    return None, value


def cp_root(name: str, path: Path) -> Path:
    """Where ``cp`` reads or writes ``path``: under a tmpfs mount it is the lower layer."""
    meta = load_meta(name) or {}
    mounts = meta.get("tmpfs")
    text = path.as_posix()
    if isinstance(mounts, list):
        for mount in mounts:
            if text == mount or text.startswith(str(mount).rstrip("/") + "/"):
                return lower_root(name)
    return fs_root(name)


def copy_local_to_container(source: Path, destination: Path, name: str) -> int:
    root = cp_root(name, destination)
    target = root / destination.as_posix().lstrip("/")
    if source.is_dir() and not source.is_symlink():
        if target.exists() and target.is_dir():
            target = target / source.name
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        return 0
    if target.exists() and target.is_dir():
        target = target / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(os.readlink(source), target)
    else:
        shutil.copy2(source, target)
    return 0


def copy_container_to_local(source: Path, destination: Path, name: str) -> int:
    root = cp_root(name, source)
    origin = root / source.as_posix().lstrip("/")
    if not origin.exists() and not origin.is_symlink():
        print(
            f'Error: failed to copy from container {name} (cause: "notFound: "copy: path '
            f"not found '{source}'\"\")",
            file=sys.stderr,
        )
        return 1
    target = destination
    if origin.is_dir() and not origin.is_symlink():
        if target.exists() and target.is_dir():
            target = target / origin.name
        shutil.copytree(origin, target, symlinks=True, dirs_exist_ok=True)
        return 0
    if target.exists() and target.is_dir():
        target = target / origin.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if origin.is_symlink():
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(os.readlink(origin), target)
    else:
        shutil.copy2(origin, target)
    return 0


def cmd_cp(args: list[str]) -> int:
    values = [arg for arg in args if not arg.startswith("-")]
    if len(values) != 2:
        print("container: cp needs a source and a destination", file=sys.stderr)
        return 2
    src_container, src_path = parse_ref(values[0])
    dst_container, dst_path = parse_ref(values[1])
    if src_container and dst_container:
        print("container: cp between containers is not supported", file=sys.stderr)
        return 2
    if src_container:
        meta = load_meta(src_container)
        if meta is None:
            print(f"container {src_container!r} not found", file=sys.stderr)
            return 1
        return copy_container_to_local(Path(src_path), Path(dst_path), src_container)
    if dst_container:
        meta = load_meta(dst_container)
        if meta is None:
            print(f"container {dst_container!r} not found", file=sys.stderr)
            return 1
        if not Path(src_path).exists() and not Path(src_path).is_symlink():
            print(f"cp: {src_path}: No such file or directory", file=sys.stderr)
            return 1
        return copy_local_to_container(Path(src_path), Path(dst_path), dst_container)
    print("container: cp needs one container reference", file=sys.stderr)
    return 2


def cmd_rm(args: list[str]) -> int:
    force = any(arg in ("-f", "--force") for arg in args)
    names = [arg for arg in args if not arg.startswith("-")]
    if "--all" in args or "-a" in args:
        names = [entry.name for entry in containers_root().iterdir() if entry.is_dir()]
    status = 0
    for name in names:
        if load_meta(name) is None:
            if not force:
                print(f"container {name!r} not found", file=sys.stderr)
                status = 1
            continue
        shutil.rmtree(containers_root() / name, ignore_errors=True)
    return status


def cmd_list(args: list[str]) -> int:
    quiet = any(arg in ("-q", "--quiet") for arg in args)
    as_json = "json" in args
    entries = []
    for directory in sorted(containers_root().iterdir()):
        if not directory.is_dir():
            continue
        meta = load_meta(directory.name)
        if meta is None:
            continue
        entries.append(
            {
                "configuration": {
                    "id": meta["id"],
                    "image": image_object(str(meta["image"])),
                    "labels": meta["labels"],
                },
                "id": meta["id"],
                "status": {"state": meta["state"]},
            }
        )
    if as_json and not quiet:
        print(json.dumps(entries))
        return 0
    for entry in entries:
        print(entry["id"])
    return 0


def cmd_inspect(args: list[str]) -> int:
    names = [arg for arg in args if not arg.startswith("-")]
    if not names:
        print("container: inspect needs a container id", file=sys.stderr)
        return 2
    entries = []
    for name in names:
        meta = load_meta(name)
        if meta is None:
            print(f"Error: container not found: {name}", file=sys.stderr)
            return 1
        entries.append(
            {
                "configuration": {
                    "id": meta["id"],
                    "image": image_object(str(meta["image"])),
                    "labels": meta["labels"],
                    "resources": {
                        "cpus": meta["cpus"],
                        "memoryInBytes": int(str(meta["memory_mb"])) << 20,
                    },
                },
                "id": meta["id"],
                "status": {"state": meta["state"]},
            }
        )
    print(json.dumps(entries))
    return 0


def networks_path() -> Path:
    return state_root() / "networks.json"


def load_networks() -> dict[str, dict[str, object]]:
    path = networks_path()
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def cmd_network(args: list[str]) -> int:
    if not args:
        return 2
    subcommand = args[0]
    if subcommand == "create":
        internal = "--internal" in args
        names = [arg for arg in args[1:] if not arg.startswith("-")]
        if not names:
            return 2
        name = names[-1]
        networks = load_networks()
        if name in networks:
            print(f"network {name!r} already exists", file=sys.stderr)
            return 1
        networks[name] = {"name": name, "internal": internal}
        networks_path().write_text(json.dumps(networks), encoding="utf-8")
        return 0
    if subcommand in ("list", "ls"):
        networks = load_networks()
        if "json" in args:
            print(json.dumps(list(networks.values())))
        else:
            for name in networks:
                print(name)
        return 0
    if subcommand in ("delete", "rm"):
        networks = load_networks()
        for name in [arg for arg in args[1:] if not arg.startswith("-")]:
            networks.pop(name, None)
        networks_path().write_text(json.dumps(networks), encoding="utf-8")
        return 0
    return 2


def cmd_system(args: list[str]) -> int:
    if args[:1] == ["version"]:
        if "json" in args:
            components = [
                {"appName": app, "buildType": "release", "commit": "fake", "version": VERSION}
                for app in ("container", "container-apiserver")
            ]
            print(json.dumps(components))
        else:
            print(f"container CLI version {VERSION}")
        return 0
    if args[:1] == ["status"] and "json" in args:
        app_root = fake_app_root()
        print(
            json.dumps(
                {
                    "client": {"appName": "container", "build": "release", "version": VERSION},
                    "host": {"architecture": "arm64", "cpus": os.cpu_count()},
                    "paths": {"appRoot": f"{app_root}/", "installRoot": f"{state_root()}/"},
                    "resources": {"containersRunning": len(list(containers_root().iterdir()))},
                    "server": {
                        "appName": "container-apiserver",
                        "build": "release",
                        "version": VERSION,
                    },
                    "status": "running",
                }
            )
        )
        return 0
    if args[:2] == ["property", "list"] and "json" in args:
        print(
            json.dumps(
                {
                    "kernel": {"binaryPath": f"opt/kata/share/kata-containers/{KERNEL_IMAGE}"},
                    "vminit": {"image": VMINIT_IMAGE},
                }
            )
        )
        return 0
    print(f"container: unsupported system command {args}", file=sys.stderr)
    return 2


def fake_app_root() -> Path:
    """The fake app root with a default kernel link, as the real CLI installs it."""
    root = state_root() / "app"
    kernels = root / "kernels"
    kernels.mkdir(parents=True, exist_ok=True)
    image = kernels / KERNEL_IMAGE
    if not image.exists():
        image.write_bytes(
            b"\x00" * 4096 + f"Linux version {KERNEL_RELEASE} (@fake) #1 SMP\n".encode() + b"\x00"
        )
    link = kernels / "default.kernel-arm64"
    if not link.is_symlink():
        link.symlink_to(image)
    return root


# -- guest command wrappers ----------------------------------------------------


def mapped(path: str, root: Path, prefixes: tuple[str, ...]) -> str:
    if path.startswith(str(state_root()) + "/"):
        return path  # already a host path inside the fake guest
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return str(root / path.lstrip("/"))
    return path


def wrap_cat(fs: Path, args: list[str]) -> int:
    mapped_args = [mapped(arg, fs, _MAPPED_PREFIXES) for arg in args]
    return subprocess.run(["/bin/cat", *mapped_args], check=False).returncode


def tmpfs_mounts(meta: dict[str, object]) -> list[str]:
    mounts = meta.get("tmpfs")
    return [str(mount) for mount in mounts] if isinstance(mounts, list) else []


def is_host_path(path: str) -> bool:
    return path.startswith(str(state_root()) + "/")


def read_only_violation(meta: dict[str, object], guest: str) -> str | None:
    """The top-level path a write to ``guest`` hits on a read-only root, or None."""
    if not meta.get("read_only") or is_host_path(guest):
        return None
    for mount in tmpfs_mounts(meta):
        if guest == mount or guest.startswith(mount.rstrip("/") + "/"):
            return None
    first = guest.strip("/").split("/", 1)[0]
    return f"/{first}"


def wrap_mkdir(fs: Path, meta: dict[str, object], args: list[str]) -> int:
    for arg in args:
        # An existing directory needs no write, so 'mkdir -p /' succeeds on a read-only root.
        if arg.startswith("/") and not (fs / arg.lstrip("/")).is_dir():
            blocked = read_only_violation(meta, arg)
            if blocked is not None:
                print(
                    f"mkdir: cannot create directory '{blocked}': Read-only file system",
                    file=sys.stderr,
                )
                return 1
    mapped_args = [
        str(fs / arg.lstrip("/")) if arg.startswith("/") and not is_host_path(arg) else arg
        for arg in args
    ]
    return subprocess.run(["/bin/mkdir", *mapped_args], check=False).returncode


def host_tool(tool: str) -> str:
    """The host's own ``tool``, skipping this fake's wrapper directory."""
    wrapper_dir = str(state_root() / "bin")
    search = os.pathsep.join(
        entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry != wrapper_dir
    )
    found = shutil.which(tool, path=search)
    if found is None:
        raise SystemExit(f"{tool}: not found on the host")
    return found


def wrap_tar(fs: Path, meta: dict[str, object], args: list[str]) -> int:
    """Host tar inside the fake guest, with the guest's read-only root and mount points."""
    args = list(args)
    target = os.getcwd()
    for position, arg in enumerate(args[:-1]):
        if arg == "-C":
            guest = args[position + 1]
            if guest.startswith("/") and not is_host_path(guest):
                args[position + 1] = str(fs / guest.lstrip("/"))
            target = args[position + 1]
    extracting = any(
        arg.startswith("-") and not arg.startswith("--") and "x" in arg for arg in args
    )
    if not extracting:
        return subprocess.run([host_tool("tar"), *args], check=False).returncode
    payload = sys.stdin.buffer.read()
    mounts = {os.path.normpath(fs / mount.lstrip("/")): mount for mount in tmpfs_mounts(meta)}
    errors: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive.getmembers():
            destination = os.path.normpath(os.path.join(target, member.name))
            name = member.name.rstrip("/")
            if member.isdir() and destination in mounts:
                # The mount point is root's: the sandbox user cannot set its times or mode.
                errors.append(f"tar: {name}: Cannot utime: Operation not permitted")
                errors.append(
                    f"tar: {name}: Cannot change mode to rwxrwxrwt: Operation not permitted"
                )
            elif meta.get("read_only") and not any(
                destination.startswith(mount_path + "/") for mount_path in mounts
            ):
                errors.append(f"tar: {name}: Cannot open: Read-only file system")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print("tar: Exiting with failure status due to previous errors", file=sys.stderr)
        return 2
    return subprocess.run([host_tool("tar"), *args], input=payload, check=False).returncode


def wrap_findmnt(args: list[str]) -> int:
    joined = " ".join(args)
    target = next((arg for arg in reversed(args) if not arg.startswith("-")), "")
    if target and "FSTYPE" in joined and "TARGET," not in joined:
        print("tmpfs" if target in ("/workspace", "/tmp") else "ext4")
        return 0
    if target and "OPTIONS" in joined and "TARGET," not in joined:
        print("ro,relatime" if target in ("", "/") else "rw,nosuid,nodev")
        return 0
    print("/ /dev/root ext4 ro,relatime")
    print("/tmp tmpfs tmpfs rw,nosuid,nodev")
    print("/workspace tmpfs tmpfs rw,exec,nosuid,nodev")
    print("/home/agent tmpfs tmpfs rw,exec,nosuid,nodev")
    return 0


def wrap_mount() -> int:
    print("/dev/root on / type ext4 (ro,relatime)")
    print("tmpfs on /tmp type tmpfs (rw,nosuid,nodev)")
    print("tmpfs on /workspace type tmpfs (rw,exec,nosuid,nodev)")
    return 0


def wrap_id(args: list[str]) -> int:
    name_mode = "-n" in args or "-un" in args or "-gn" in args
    if "-u" in args or "-un" in args:
        print("agent" if name_mode else "1000")
        return 0
    if "-g" in args or "-gn" in args:
        print("agent" if name_mode else "1000")
        return 0
    print("uid=1000(agent) gid=1000(agent) groups=1000(agent)")
    return 0


def wrap_tool(tool: str, args: list[str]) -> int:
    name = os.environ.get("SPLEEF_FAKE_CONTAINER_ID", "")
    meta = load_meta(name) if name else None
    if meta is None:
        print(f"{tool}: no container context", file=sys.stderr)
        return 1
    fs = fs_root(name)
    if tool == "cat":
        return wrap_cat(fs, args)
    if tool == "mkdir":
        return wrap_mkdir(fs, meta, args)
    if tool == "tar":
        return wrap_tar(fs, meta, args)
    if tool == "findmnt":
        return wrap_findmnt(args)
    if tool == "mount":
        return wrap_mount()
    if tool == "id":
        return wrap_id(args)
    if tool == "whoami":
        print("agent")
        return 0
    if tool == "sudo":
        print("sudo: a password is required", file=sys.stderr)
        return 1
    if tool == "nproc":
        print(str(int(float(str(meta.get("cpus") or 1)))))
        return 0
    if tool == "hostname":
        print(name)
        return 0
    print(f"{tool}: not implemented", file=sys.stderr)
    return 127


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    if argv[0] == "__wrap__":
        return wrap_tool(argv[1], argv[2:])
    command = argv[0]
    rest = argv[1:]
    if command == "run":
        return cmd_run(rest)
    if command == "create":
        return cmd_run(rest, start=False)
    if command == "exec":
        return cmd_exec(rest)
    if command in ("cp", "copy"):
        return cmd_cp(rest)
    if command in ("rm", "delete"):
        return cmd_rm(rest)
    if command in ("ls", "list"):
        return cmd_list(rest)
    if command == "inspect":
        return cmd_inspect(rest)
    if command == "network":
        return cmd_network(rest)
    if command == "system":
        return cmd_system(rest)
    if command in ("--version", "version"):
        print(f"container CLI version {VERSION} (build: release, commit: fake)")
        return 0
    print(f"container: unsupported command {command}", file=sys.stderr)
    return 2


def install(directory: Path, *, python: str | None = None) -> Path:
    """Copy this script into ``directory`` as an executable named ``container``."""
    source = Path(__file__).resolve()
    target = Path(directory) / "container"
    text = source.read_text(encoding="utf-8")
    if python is not None:
        first_line, _, rest = text.partition("\n")
        if first_line.startswith("#!"):
            text = f"#!{python}\n{rest}"
    target.write_text(text, encoding="utf-8")
    target.chmod(0o755)
    return target


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
