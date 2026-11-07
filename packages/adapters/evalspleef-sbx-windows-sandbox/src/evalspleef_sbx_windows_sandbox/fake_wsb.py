#!/usr/bin/env python3
"""A fake ``wsb`` executable for tests and the conformance target.

Windows Sandbox only exists on Windows, so nothing in this package is verified
on a real host. This script emulates the three commands the adapter uses over a
local directory tree:

``wsb start --config <xml>``
    Creates ``<state>/instances/<id>/`` from the ``HostFolder`` of the first
    mapped folder in the config XML and prints the instance id.
``wsb exec --id <id> -c <cmd> -r ExistingLogin``
    Runs ``cmd`` with ``/bin/sh -c`` in the mapped folder and exits with the
    command's exit code. Like the real CLI it never echoes stdout: the adapter
    redirects output to files inside the mapped folder.
``wsb stop --id <id>``
    Removes the instance directory (a missing instance still succeeds).

The state root comes from ``SPLEEF_WSB_STATE``; the adapter sets it for every
child process. ``install()`` copies this file to a temporary directory as
``wsb`` so the adapter can resolve it through ``PATH``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path


def _state_root() -> Path:
    raw = os.environ.get("SPLEEF_WSB_STATE")
    if not raw:
        sys.exit("fake wsb: SPLEEF_WSB_STATE is not set")
    return Path(raw)


def _instance_dir(instance_id: str) -> Path:
    return _state_root() / "instances" / instance_id


def _start(config: Path) -> int:
    try:
        root = ET.parse(config).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"fake wsb: cannot read config {config}: {exc}", file=sys.stderr)
        return 2
    host_folder = root.findtext("./MappedFolders/MappedFolder/HostFolder")
    if not host_folder:
        print("fake wsb: config has no mapped host folder", file=sys.stderr)
        return 2
    workspace = Path(host_folder)
    workspace.mkdir(parents=True, exist_ok=True)
    instance_id = f"wsb-{uuid.uuid4().hex[:12]}"
    directory = _instance_dir(instance_id)
    directory.mkdir(parents=True)
    networking = root.findtext("./Networking") or "Disable"
    (directory / "meta.json").write_text(
        json.dumps({"workspace": str(workspace), "networking": networking}),
        encoding="utf-8",
    )
    print(instance_id)
    return 0


def _exec(instance_id: str, command: str) -> int:
    directory = _instance_dir(instance_id)
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        print(f"fake wsb: unknown instance {instance_id!r}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    workspace = Path(str(meta["workspace"]))
    if not workspace.is_dir():
        print(f"fake wsb: workspace {workspace} is gone", file=sys.stderr)
        return 2
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=workspace, check=False)
    return int(completed.returncode)


def _stop(instance_id: str) -> int:
    shutil.rmtree(_instance_dir(instance_id), ignore_errors=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wsb")
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command", required=False)

    start = sub.add_parser("start")
    start.add_argument("--config", required=True, type=Path)

    exec_parser = sub.add_parser("exec")
    exec_parser.add_argument("--id", required=True)
    # ``dest`` must not collide with the subparsers' ``command`` destination.
    exec_parser.add_argument("-c", "--command", dest="command_text", required=True)
    exec_parser.add_argument("-r", "--login", default="ExistingLogin")

    stop = sub.add_parser("stop")
    stop.add_argument("--id", required=True)

    args = parser.parse_args(argv)
    if args.version:
        print("fake-wsb 0.1.0")
        return 0
    if args.command == "start":
        return _start(args.config)
    if args.command == "exec":
        return _exec(args.id, args.command_text)
    if args.command == "stop":
        return _stop(args.id)
    parser.print_help(sys.stderr)
    return 2


def install(directory: Path) -> Path:
    """Copy this script to ``directory/wsb`` (executable) and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "wsb"
    shutil.copyfile(Path(__file__), target)
    target.chmod(0o755)
    return target


if __name__ == "__main__":
    sys.exit(main())
