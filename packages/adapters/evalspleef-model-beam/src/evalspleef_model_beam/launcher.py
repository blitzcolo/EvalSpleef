"""Child-process entry point that deploys a Beam Pod through the Beam SDK.

The SDK (``beam-client``) pins protobuf<7 while the workspace needs protobuf>=7, so this file
runs in its own uv environment: ``uv run --no-project --with beam-client==<pin> python -I
launcher.py``. The provider starts it in an empty working directory, so the SDK has no local
files to sync and writes its ignore file into a scratch directory, with the Beam token only in
the child's environment. The request arrives as JSON on stdin (not argv, so secret values stay
out of the process table) and the result is the last stdout line that starts with
``RESULT_PREFIX``; everything else on stdout is the SDK's own terminal output.

Standard library only, apart from the SDK, which is imported at run time.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping
from typing import Any

RESULT_PREFIX = "SPLEEF_BEAM_RESULT "


def build_pod(sdk: Any, request: Mapping[str, Any]) -> Any:
    """The SDK ``Pod`` for one request. The URL always requires a Beam token."""
    secrets = [str(name) for name in request.get("secrets") or []]
    volumes = [
        sdk.Volume(name=str(volume["name"]), mount_path=str(volume["mount_path"]))
        for volume in request.get("volumes") or []
    ]
    return sdk.Pod(
        name=str(request["name"]),
        image=sdk.Image.from_registry(str(request["image"])),
        entrypoint=[str(arg) for arg in request["entrypoint"]],
        ports=[int(port) for port in request["ports"]],
        cpu=request["cpu"],
        memory=request["memory"],
        gpu=str(request["gpu"]),
        gpu_count=int(request["gpu_count"]),
        env={str(key): str(value) for key, value in dict(request.get("env") or {}).items()},
        secrets=secrets or None,
        volumes=volumes or None,
        keep_warm_seconds=int(request["keep_warm_seconds"]),
        authorized=True,
    )


def deploy(sdk: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Deploy the Pod and return the result record the provider parses."""
    details, ok = build_pod(sdk, request).deploy(name=str(request["name"]))
    details = details if isinstance(details, Mapping) else {}
    return {
        "ok": bool(ok),
        "deployment_id": str(details.get("deployment_id") or ""),
        "stub_id": str(details.get("stub_id") or ""),
        "invoke_url": str(details.get("invoke_url") or ""),
        "version": details.get("version"),
        "warning": str(details.get("warning") or ""),
        "error": "" if ok else "the Beam SDK reported that the deployment failed",
    }


def fetch(sdk: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Start a one-off Pod run (the weights download) and return its container and task ids."""
    instance = build_pod(sdk, request).create()
    ok = bool(getattr(instance, "ok", False))
    return {
        "ok": ok,
        "container_id": str(getattr(instance, "container_id", "") or ""),
        "task_id": str(getattr(instance, "task_id", "") or ""),
        "error": "" if ok else str(getattr(instance, "error_msg", "") or "the Pod run failed"),
    }


def run(sdk: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch on ``request['action']``: ``deploy`` (the default) or ``fetch``."""
    return fetch(sdk, request) if request.get("action") == "fetch" else deploy(sdk, request)


def main() -> int:
    request = json.loads(sys.stdin.read())
    try:
        result = run(importlib.import_module("beam"), request)
    except (Exception, SystemExit) as exc:
        # The SDK raises on gRPC failures and calls sys.exit() on fatal terminal errors; both
        # become a failed result the provider turns into a typed error.
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(RESULT_PREFIX + json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
