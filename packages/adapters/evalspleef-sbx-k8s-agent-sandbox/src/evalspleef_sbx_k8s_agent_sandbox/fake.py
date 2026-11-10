"""In-memory ``KubeApi`` for tests and conformance.

The fake keeps custom objects in a dict and runs pod exec in real local
subprocesses: every Sandbox object owns a temp directory that stands in for the
pod filesystem, with ``/workspace`` and ``/tmp`` mapped below it. Exit codes,
signals, process trees, streaming and file copies therefore behave like the
production transport, while the cluster (CRDs, controller, Cilium) is simulated.

This is a test double. It does not enforce Cilium network policies, Kubernetes
resource limits or RuntimeClass isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import shutil
import signal
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalspleef.spi.errors import SandboxLost, SpleefError, TransferError
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE, ExecResult

from .kube import (
    API_VERSION,
    EXTENSIONS_GROUP,
    SANDBOX_CLAIM_PLURAL,
    SANDBOX_GROUP,
    SANDBOX_PLURAL,
    SANDBOX_TEMPLATE_PLURAL,
    SANDBOX_WARMPOOL_PLURAL,
    ExecRequest,
)
from .output import CappedBuffer

_BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass
class _SandboxState:
    root: Path
    namespace: str
    name: str
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    lost: bool = False


class FakeKubeApi:
    """A ``KubeApi`` double: in-memory objects, local processes for pod exec."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        base_dir: Path | None = None,
    ) -> None:
        self._clock = clock
        self._base = Path(
            base_dir
            if base_dir is not None
            else f"{Path(os.environ.get('TMPDIR', '/tmp'))}/spleef-k8s-{uuid.uuid4().hex[:12]}"
        )
        self._objects: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._states: dict[tuple[str, str], _SandboxState] = {}
        self._closed = False

    # -- object store ---------------------------------------------------------

    def _key(
        self, group: str, version: str, plural: str, namespace: str, name: str
    ) -> tuple[str, str, str, str, str]:
        return (group, version, plural, namespace, name)

    async def create_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        stored = copy.deepcopy(dict(body))
        metadata = stored.setdefault("metadata", {})
        name = str(metadata.get("name", ""))
        if not name:
            raise SpleefError("custom object has no metadata.name")
        metadata.setdefault("namespace", namespace)
        metadata.setdefault("labels", {})
        metadata.setdefault("annotations", {})
        metadata.setdefault(
            "creationTimestamp",
            datetime.fromtimestamp(self._clock(), tz=UTC).isoformat(),
        )
        key = self._key(group, version, plural, namespace, name)
        if key in self._objects:
            raise SpleefError(f"custom object already exists: {plural}/{name}")
        stored.setdefault("apiVersion", f"{group}/{version}")
        if plural == SANDBOX_PLURAL:
            stored.setdefault("status", _ready_status())
            self._objects[key] = stored
            self._ensure_state(namespace, name, stored)
        else:
            self._objects[key] = stored
        if plural == SANDBOX_CLAIM_PLURAL:
            self._fulfil_claim(namespace, stored)
        return copy.deepcopy(self._objects[key])

    async def get_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        found = self._objects.get(self._key(group, version, plural, namespace, name))
        return None if found is None else copy.deepcopy(found)

    async def delete_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> None:
        found = self._objects.pop(self._key(group, version, plural, namespace, name), None)
        if plural == SANDBOX_PLURAL:
            state = self._states.pop((namespace, name), None)
            if state is not None:
                shutil.rmtree(state.root, ignore_errors=True)
        if found is None:
            return

    async def list_objects(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        label_selector: str,
    ) -> list[dict[str, Any]]:
        wanted = _parse_selector(label_selector)
        items: list[dict[str, Any]] = []
        for (
            object_group,
            object_version,
            object_plural,
            object_ns,
            _,
        ), body in self._objects.items():
            if (
                object_group != group
                or object_version != version
                or object_plural != plural
                or object_ns != namespace
            ):
                continue
            labels = body.get("metadata", {}).get("labels", {})
            if all(str(labels.get(key)) == value for key, value in wanted.items()):
                items.append(copy.deepcopy(body))
        return items

    def _ensure_state(self, namespace: str, name: str, body: Mapping[str, Any]) -> _SandboxState:
        existing = self._states.get((namespace, name))
        if existing is not None:
            return existing
        root = self._base / f"{namespace}-{name}"
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "home").mkdir(parents=True, exist_ok=True)
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        metadata = body.get("metadata", {})
        state = _SandboxState(
            root=root,
            namespace=namespace,
            name=name,
            labels=dict(metadata.get("labels") or {}),
            env=_pod_env(body),
        )
        self._states[(namespace, name)] = state
        return state

    def _fulfil_claim(self, namespace: str, claim: dict[str, Any]) -> None:
        """Mimic the controller: claim a pre-warmed Sandbox from the named pool."""
        spec = claim.get("spec", {})
        pool_ref = spec.get("warmPoolRef", {}).get("name")
        if not pool_ref:
            return
        pool = self._objects.get(
            self._key(EXTENSIONS_GROUP, API_VERSION, SANDBOX_WARMPOOL_PLURAL, namespace, pool_ref)
        )
        if pool is None:
            return
        template_ref = pool.get("spec", {}).get("sandboxTemplateRef", {}).get("name")
        template = (
            None
            if not template_ref
            else self._objects.get(
                self._key(
                    EXTENSIONS_GROUP,
                    API_VERSION,
                    SANDBOX_TEMPLATE_PLURAL,
                    namespace,
                    template_ref,
                )
            )
        )
        name = str(claim.get("metadata", {}).get("name", ""))
        if not name:
            return
        labels = dict(claim.get("metadata", {}).get("labels") or {})
        sandbox: dict[str, Any] = {
            "apiVersion": f"{SANDBOX_GROUP}/{API_VERSION}",
            "kind": "Sandbox",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    "spleef.io/claimed-from": pool_ref,
                },
            },
            "spec": copy.deepcopy((template or {}).get("spec", {})),
            "status": _ready_status(),
        }
        key = self._key(SANDBOX_GROUP, API_VERSION, SANDBOX_PLURAL, namespace, name)
        self._objects[key] = sandbox
        self._ensure_state(namespace, name, sandbox)
        claim.setdefault("status", {})["sandboxName"] = name

    # -- exec -----------------------------------------------------------------

    def state(self, namespace: str, name: str) -> _SandboxState:
        """The local filesystem state behind one sandbox (test inspection)."""
        state = self._states.get((namespace, name))
        if state is None:
            raise SandboxLost(detail={"sandbox_id": name})
        return state

    async def exec_in_pod(self, request: ExecRequest) -> ExecResult:
        state = self._states.get((request.namespace, request.pod))
        if state is None or state.lost:
            raise SandboxLost(detail={"sandbox_id": request.pod})
        return await self._exec_process(state, request)

    async def _exec_process(self, state: _SandboxState, request: ExecRequest) -> ExecResult:
        argv = (
            ["/bin/sh", "-lc", request.argv[0]]
            if len(request.argv) == 1
            else [str(item) for item in request.argv]
        )
        cwd = self._map_dir(state, request.cwd or "/workspace")
        if not cwd.is_dir():
            raise TransferError(f"cwd does not exist in sandbox: {request.cwd}")
        full_env = dict(_BASE_ENV)
        full_env["HOME"] = str(state.root / "home")
        full_env["TMPDIR"] = str(state.root / "tmp")
        full_env.update(state.env)
        full_env.update(request.env)
        stdout = CappedBuffer(request.max_output_bytes)
        stderr = CappedBuffer(request.max_output_bytes)
        started = time.monotonic()

        async def pump(name: str, buffer: CappedBuffer) -> None:
            stream = proc.stdout if name == "stdout" else proc.stderr
            assert stream is not None
            while True:
                data = await stream.read(65536)
                if not data:
                    return
                buffer.append(data)
                if request.on_output is not None:
                    request.on_output("stdout" if name == "stdout" else "stderr", data)

        async def feed() -> None:
            if request.stdin is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(request.stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=full_env,
            stdin=asyncio.subprocess.PIPE
            if request.stdin is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        pumps = [
            asyncio.create_task(pump("stdout", stdout)),
            asyncio.create_task(pump("stderr", stderr)),
            asyncio.create_task(feed()),
        ]
        timed_out = False
        try:
            if request.timeout_s is not None:
                await asyncio.wait_for(asyncio.gather(*pumps), timeout=request.timeout_s)
            else:
                await asyncio.gather(*pumps)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await asyncio.gather(*pumps, return_exceptions=True)
        finally:
            for task in pumps:
                task.cancel()
        exit_code = await proc.wait()
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=time.monotonic() - started,
            truncated=stdout.truncated or stderr.truncated,
        )

    # -- file copy ------------------------------------------------------------

    async def copy_to_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: Path,
        destination: str,
    ) -> None:
        del container
        state = self._live_state(namespace, pod)
        remote = self._map(state, destination)
        await asyncio.to_thread(_copy_into, Path(source), remote)

    async def copy_from_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: str,
        destination: Path,
        max_bytes: int | None,
    ) -> None:
        del container
        state = self._live_state(namespace, pod)
        remote = self._map_existing(state, source)
        await asyncio.to_thread(_copy_out, remote, Path(destination), max_bytes)

    def _live_state(self, namespace: str, name: str) -> _SandboxState:
        state = self._states.get((namespace, name))
        if state is None or state.lost:
            raise SandboxLost(detail={"sandbox_id": name})
        return state

    def _map(self, state: _SandboxState, remote: str) -> Path:
        if not remote.startswith("/"):
            remote = f"/{remote}"
        root = Path(os.path.realpath(state.root))
        mapped = Path(os.path.normpath(str(root / remote.lstrip("/"))))
        if os.path.commonpath([str(root), str(mapped)]) != str(root):
            raise TransferError(f"path escapes sandbox root: {remote}")
        return mapped

    def _map_dir(self, state: _SandboxState, remote: str) -> Path:
        return self._map(state, remote)

    def _map_existing(self, state: _SandboxState, remote: str) -> Path:
        mapped = self._map(state, remote)
        resolved = Path(os.path.realpath(mapped))
        root = Path(os.path.realpath(state.root))
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise TransferError(f"symlink escapes sandbox root: {remote}")
        return mapped

    # -- test controls --------------------------------------------------------

    async def lose(self, namespace: str, name: str) -> None:
        """Simulate the vendor reclaiming a sandbox: the object and pod vanish."""
        state = self._states.pop((namespace, name), None)
        if state is not None:
            state.lost = True
            shutil.rmtree(state.root, ignore_errors=True)
        self._objects.pop(self._key(SANDBOX_GROUP, API_VERSION, SANDBOX_PLURAL, namespace, name))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for state in self._states.values():
            shutil.rmtree(state.root, ignore_errors=True)
        self._states.clear()
        self._objects.clear()
        shutil.rmtree(self._base, ignore_errors=True)


def _ready_status() -> dict[str, Any]:
    return {
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "Ready"},
        ]
    }


def _pod_env(body: Mapping[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    pod_spec = body.get("spec", {}).get("podTemplate", {}).get("spec", {})
    containers = pod_spec.get("containers") or []
    if containers:
        for item in containers[0].get("env") or []:
            if "name" in item and "value" in item:
                env[str(item["name"])] = str(item["value"])
    return env


def _parse_selector(selector: str) -> dict[str, str]:
    wanted: dict[str, str] = {}
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition("=")
        wanted[key] = value
    return wanted


def _copy_into(local: Path, remote: Path) -> None:
    """Copy an uploaded file or directory into the fake pod root."""
    if not local.exists() and not local.is_symlink():
        raise TransferError(f"local path does not exist: {local}")
    if local.is_symlink():
        target = local.resolve()
        if not target.is_relative_to(local.parent.resolve()):
            raise TransferError(f"refusing symlink that escapes the uploaded tree: {local}")
    remote.parent.mkdir(parents=True, exist_ok=True)
    if local.is_dir():
        # Pod copies are tar extractions: they merge into an existing directory.
        shutil.copytree(
            local, remote, symlinks=True, copy_function=shutil.copy2, dirs_exist_ok=True
        )
    else:
        shutil.copy2(local, remote)


def _copy_out(remote: Path, local: Path, max_bytes: int | None) -> None:
    """Copy a downloaded path out of the fake pod root, sanitising symlinks."""
    if remote.is_dir():
        total = _tree_size(remote)
        if max_bytes is not None and total > max_bytes:
            raise TransferError(f"download exceeds max_bytes: {total} > {max_bytes}")
        if local.exists():
            shutil.rmtree(local)
        _copy_tree_sanitized(remote, local)
    else:
        if max_bytes is not None and remote.stat().st_size > max_bytes:
            raise TransferError(f"download exceeds max_bytes: {remote.stat().st_size}")
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote, local)


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def _copy_tree_sanitized(src: Path, dest: Path) -> None:
    """Copy a directory, refusing symlinks that point outside ``src``."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copystat(src, dest, follow_symlinks=False)
    for entry in src.iterdir():
        target = dest / entry.name
        if entry.is_symlink():
            resolved = Path(os.path.realpath(entry))
            if os.path.commonpath([str(src.resolve()), str(resolved)]) != str(src.resolve()):
                raise TransferError(f"refusing symlink outside the sandbox: {entry}")
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            _copy_tree_sanitized(entry, target)
        else:
            shutil.copy2(entry, target)
