"""An in-memory fake of the E2B-family SDK surface the adapter uses.

The fake mirrors the *signatures* of the real ``novita-sandbox`` 2.x and ``e2b``
SDK entry points (a unit test compares parameter names against the installed
SDKs) and executes commands as real local subprocesses inside a per-sandbox
temporary directory that emulates the sandbox filesystem — ``/workspace``
inside commands maps onto ``<sandbox-root>/workspace``, so absolute paths
behave as they would inside a real sandbox without touching the host's real
``/workspace``.

Differences from the real SDK, by design:

* the fake cannot truly isolate or enforce network policy; ``off``/allowlist
  modes are modelled by pointing ``http_proxy``/``https_proxy``/``all_proxy``
  at a closed loopback port, so any outbound HTTP(S) fails deterministically;
* the fake runs commands with ``/bin/bash -c`` instead of the envd login shell
  ``/bin/bash -l -c`` — the operator's workstation profile is not representative
  of a template image, and the adapter's ``env -i`` wrapper is applied on top
  either way.

Everything else — create with template/timeout/metadata/network options,
background commands with pid/wait/kill, exit-code exceptions, files
write/read, sandbox kill, label-based listing with pagination, snapshots —
follows the real SDK semantics closely enough to run the conformance kit's
fake targets.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types as _types
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_CHUNK_BYTES = 64 * 1024

#: Loopback address with a closed port: any proxied connection fails fast.
_BLACKHOLE_PROXY = "http://127.0.0.1:9"

WORKSPACE = "/workspace"


# ---------------------------------------------------------------------------
# Fake vendor exception hierarchy (mirrors the real SDK shapes)
# ---------------------------------------------------------------------------


class FakeSandboxException(Exception):
    """Base class of the fake vendor error hierarchy (cf. ``SandboxException``)."""


class FakeSandboxNotFoundException(FakeSandboxException):
    """A sandbox id does not exist (or it has been lost / expired)."""


class FakeRateLimitException(FakeSandboxException):
    """The vendor rate limit or quota was hit."""


class FakeFileNotFoundException(FakeSandboxException):
    """A filesystem read targeted a missing path."""


class FakeCommandResult:
    """Mirrors ``novita_sandbox``/``e2b`` ``CommandResult`` (str output)."""

    def __init__(self, *, stdout: str, stderr: str, exit_code: int, error: str | None = None):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error


class FakeCommandExitException(FakeSandboxException):
    """Raised by ``FakeCommandHandle.wait`` on a non-zero exit, like the real SDK.

    Carries the same ``stdout`` / ``stderr`` / ``exit_code`` attributes as the
    vendor ``CommandExitException``.
    """

    def __init__(self, *, stdout: str, stderr: str, exit_code: int, error: str | None = None):
        super().__init__(f"command exited with code {exit_code}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error


# ---------------------------------------------------------------------------
# Cluster: shared fake state (sandboxes, snapshots, fault injection, clocks)
# ---------------------------------------------------------------------------


class FakeSandboxRecord:
    """One fake sandbox instance."""

    def __init__(
        self,
        cluster: FakeE2BCluster,
        sandbox_id: str,
        template: str,
        timeout: int | None,
        metadata: Mapping[str, str],
        allow_internet_access: bool,
        network: Mapping[str, Any] | None,
        filesystem_root: Path,
    ) -> None:
        self.cluster = cluster
        self.sandbox_id = sandbox_id
        self.template = template
        self.timeout = timeout
        self.metadata = dict(metadata)
        self.allow_internet_access = allow_internet_access
        self.network = dict(network) if network else None
        self.filesystem_root = filesystem_root
        self.started_at_wall = cluster.wall()
        self.alive = True

    def expired(self) -> bool:
        if self.timeout is None:
            return False
        return self.cluster.wall() >= self.started_at_wall + float(self.timeout)

    def map_path(self, remote: str) -> Path:
        """Map an absolute sandbox path onto the fake filesystem root."""
        if not remote.startswith("/"):
            raise FakeFileNotFoundException(f"not an absolute path: {remote!r}")
        mapped = Path(os.path.normpath(str(self.filesystem_root / remote.lstrip("/"))))
        if os.path.commonpath([str(self.filesystem_root), str(mapped)]) != str(
            self.filesystem_root
        ):
            raise FakeFileNotFoundException(f"path escapes the sandbox filesystem: {remote!r}")
        return mapped

    def map_cwd(self, cwd: str | None) -> Path:
        """Map a command working directory; defaults to /workspace."""
        if cwd is None:
            workspace = self.map_path(WORKSPACE)
            workspace.mkdir(parents=True, exist_ok=True)
            return workspace
        mapped = self.map_path(cwd)
        mapped.mkdir(parents=True, exist_ok=True)
        return mapped

    def subprocess_env(self, environ: Mapping[str, str] | None) -> dict[str, str]:
        """Environment for spawned commands, modelling the network policy."""
        env = dict(os.environ if environ is None else environ)
        if not self.allow_internet_access:
            env["http_proxy"] = _BLACKHOLE_PROXY
            env["https_proxy"] = _BLACKHOLE_PROXY
            env["all_proxy"] = _BLACKHOLE_PROXY
            env.pop("no_proxy", None)
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            env.pop("ALL_PROXY", None)
            env.pop("NO_PROXY", None)
        elif self.network and self.network.get("allow_out"):
            env["http_proxy"] = _BLACKHOLE_PROXY
            env["https_proxy"] = _BLACKHOLE_PROXY
            env["all_proxy"] = _BLACKHOLE_PROXY
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            env.pop("ALL_PROXY", None)
            hosts = [
                entry
                for entry in self.network["allow_out"]
                if "/" not in entry and not entry.replace(".", "").isdigit()
            ]
            env["no_proxy"] = ",".join(hosts)
            env.pop("NO_PROXY", None)
        return env


class FakeE2BCluster:
    """In-memory vendor cluster: registry of sandboxes, snapshots and faults."""

    def __init__(
        self,
        *,
        wall: Callable[[], float] | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self._wall = wall or time.time
        self._base_dir = Path(base_dir) if base_dir else None
        self._sandboxes: dict[str, FakeSandboxRecord] = {}
        self._snapshots: dict[str, Path] = {}
        self._counter = 0
        self.fail_next_creates = 0
        self.fail_create_error: type[FakeSandboxException] = FakeSandboxException
        self.create_calls: list[dict[str, Any]] = []

    # -- clocks ----------------------------------------------------------------

    def wall(self) -> float:
        return self._wall()

    # -- lifecycle ---------------------------------------------------------------

    def create(
        self,
        *,
        template: str | None = None,
        timeout: int | None = None,
        metadata: Mapping[str, str] | None = None,
        allow_internet_access: bool = True,
        network: Mapping[str, Any] | None = None,
    ) -> FakeSandbox:
        if self.fail_next_creates > 0:
            self.fail_next_creates -= 1
            raise self.fail_create_error("injected create failure")
        self._counter += 1
        sandbox_id = f"sbx-{self._counter:04d}-{uuid.uuid4().hex[:8]}"
        root = Path(tempfile.mkdtemp(prefix=f"spleef-fake-{sandbox_id}-", dir=self._base_dir))
        record = FakeSandboxRecord(
            cluster=self,
            sandbox_id=sandbox_id,
            template=str(template),
            timeout=timeout,
            metadata=metadata or {},
            allow_internet_access=allow_internet_access,
            network=network,
            filesystem_root=root,
        )
        workspace = record.map_path(WORKSPACE)
        if template in self._snapshots:
            shutil.copytree(self._snapshots[template] / "workspace", workspace, symlinks=True)
        else:
            workspace.mkdir(parents=True, exist_ok=True)
        self._sandboxes[sandbox_id] = record
        self.create_calls.append(
            {
                "template": record.template,
                "timeout": timeout,
                "metadata": dict(record.metadata),
                "allow_internet_access": allow_internet_access,
                "network": dict(network) if network else None,
            }
        )
        return FakeSandbox(record)

    def get(self, sandbox_id: str) -> FakeSandboxRecord:
        record = self._sandboxes.get(sandbox_id)
        if record is None or not record.alive or record.expired():
            raise FakeSandboxNotFoundException(f"sandbox not found: {sandbox_id}")
        return record

    def peek(self, sandbox_id: str) -> FakeSandboxRecord | None:
        return self._sandboxes.get(sandbox_id)

    def kill(self, sandbox_id: str) -> bool:
        record = self._sandboxes.get(sandbox_id)
        if record is None or not record.alive:
            return False
        record.alive = False
        shutil.rmtree(record.filesystem_root, ignore_errors=True)
        return True

    def lose(self, sandbox_id: str) -> None:
        """Simulate the vendor reclaiming a live sandbox (C-FAIL-01)."""
        record = self._sandboxes.get(sandbox_id)
        if record is not None:
            record.alive = False
            shutil.rmtree(record.filesystem_root, ignore_errors=True)

    def expire(self, sandbox_id: str) -> None:
        """Force a sandbox past its TTL (C-LIFE-03)."""
        record = self._sandboxes.get(sandbox_id)
        if record is not None:
            record.started_at_wall = self._wall() - float(record.timeout or 0) - 1.0

    def expire_all(self) -> None:
        """Force every live sandbox past its TTL (C-LIFE-03 injector)."""
        for sandbox_id in list(self._sandboxes):
            self.expire(sandbox_id)

    def snapshot(self, sandbox_id: str) -> str:
        record = self.get(sandbox_id)
        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
        destination = Path(tempfile.mkdtemp(prefix=f"spleef-snap-{snapshot_id}-"))
        shutil.copytree(record.map_path(WORKSPACE), destination / "workspace", symlinks=True)
        self._snapshots[snapshot_id] = destination
        return snapshot_id

    def list(
        self, metadata: Mapping[str, str] | None = None
    ) -> list[tuple[str, dict[str, str], float]]:
        wanted = dict(metadata or {})
        entries = []
        for sandbox_id, record in self._sandboxes.items():
            if not record.alive or record.expired():
                continue
            if all(record.metadata.get(k) == v for k, v in wanted.items()):
                entries.append((sandbox_id, dict(record.metadata), record.started_at_wall))
        return entries

    def cleanup(self) -> None:
        """Remove every remaining sandbox and snapshot directory."""
        for record in self._sandboxes.values():
            shutil.rmtree(record.filesystem_root, ignore_errors=True)
            record.alive = False
        for snapshot_root in self._snapshots.values():
            shutil.rmtree(snapshot_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fake SDK objects
# ---------------------------------------------------------------------------


class FakeCommandHandle:
    """Mirrors ``CommandHandle``: pid, wait(on_stdout, on_stderr), kill."""

    def __init__(
        self,
        record: FakeSandboxRecord,
        cmd: str,
        *,
        cwd: str | None,
        stdin: bool,
        environ: Mapping[str, str] | None,
    ) -> None:
        self._record = record
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            cwd=str(record.map_cwd(cwd)),
            env=record.subprocess_env(environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            start_new_session=True,
        )
        self._pid = int(self._proc.pid)
        self._threads = [
            threading.Thread(
                target=self._reader, args=(self._proc.stdout, self._stdout_parts), daemon=True
            ),
            threading.Thread(
                target=self._reader, args=(self._proc.stderr, self._stderr_parts), daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    @property
    def pid(self) -> int:
        return self._pid

    def _reader(self, pipe: Any, parts: list[str]) -> None:
        while True:
            data = pipe.read(_CHUNK_BYTES)
            if not data:
                break
            parts.append(data.decode("utf-8", errors="replace"))

    def send_stdin(self, data: str) -> None:
        if self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data.encode("utf-8", errors="replace"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def wait(
        self,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> FakeCommandResult:
        exit_code = self._proc.wait()
        for thread in self._threads:
            thread.join(timeout=10.0)
        stdout = "".join(self._stdout_parts)
        stderr = "".join(self._stderr_parts)
        if on_stdout is not None and stdout:
            on_stdout(stdout)
        if on_stderr is not None and stderr:
            on_stderr(stderr)
        result = FakeCommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        if exit_code != 0:
            raise FakeCommandExitException(
                stdout=stdout, stderr=stderr, exit_code=exit_code, error=result.error
            )
        return result

    def kill(self) -> bool:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pid, 9)
        return True


class FakeCommands:
    """Mirrors the SDK ``commands`` module."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox

    def run(
        self,
        cmd: str,
        background: bool | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        stdin: bool | None = None,
        timeout: float | None = 60,
        request_timeout: float | None = None,
    ) -> Any:
        del user, timeout, request_timeout
        record = self._sandbox._record()
        handle = FakeCommandHandle(record, cmd, cwd=cwd, stdin=bool(stdin), environ=envs)
        self._sandbox._handles[handle.pid] = handle
        if background:
            return handle
        return handle.wait(on_stdout=on_stdout, on_stderr=on_stderr)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        del request_timeout
        handle = self._sandbox._handles.get(int(pid))
        if handle is None:
            return False
        return handle.kill()

    def send_stdin(self, pid: int, data: str, request_timeout: float | None = None) -> None:
        del request_timeout
        handle = self._sandbox._handles.get(int(pid))
        if handle is None:
            raise FakeSandboxNotFoundException(f"no such command pid: {pid}")
        handle.send_stdin(data)


class FakeFilesystem:
    """Mirrors the SDK ``files`` module (write creates parent directories)."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox

    def write(
        self,
        path: str,
        data: str | bytes,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        del user, request_timeout
        record = self._sandbox._record()
        mapped = record.map_path(path)
        mapped.parent.mkdir(parents=True, exist_ok=True)
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        mapped.write_bytes(payload)
        return {"path": path, "size": len(payload)}

    def read(
        self,
        path: str,
        format: str = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        del user, request_timeout
        record = self._sandbox._record()
        mapped = record.map_path(path)
        if mapped.is_symlink() or not mapped.exists() or not mapped.is_file():
            raise FakeFileNotFoundException(f"file not found: {path}")
        payload = mapped.read_bytes()
        if format == "text":
            return payload.decode("utf-8", errors="replace")
        if format == "bytes":
            return bytearray(payload)
        if format == "stream":
            return iter([payload])
        raise ValueError(f"unknown read format: {format!r}")


class FakeSnapshotInfo:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id


class FakeSandbox:
    """Mirrors the SDK ``Sandbox`` object and its class-level API."""

    def __init__(self, record: FakeSandboxRecord) -> None:
        self._record_ref = record
        self.sandbox_id = record.sandbox_id
        self._commands = FakeCommands(self)
        self._files = FakeFilesystem(self)
        self._handles: dict[int, FakeCommandHandle] = {}

    def _record(self) -> FakeSandboxRecord:
        # Every instance call goes through the cluster so that lost/expired
        # sandboxes raise the not-found error like the real SDK would.
        return self._record_ref.cluster.get(self.sandbox_id)

    @property
    def commands(self) -> FakeCommands:
        return self._commands

    @property
    def files(self) -> FakeFilesystem:
        return self._files

    def kill(self) -> bool:
        return self._record_ref.cluster.kill(self.sandbox_id)

    # Alias used by the class-level `kill` variant of the cluster-bound
    # subclass (mirrors the SDKs' class_method_variant dispatch).
    _instance_kill = kill

    def is_running(self) -> bool:
        record = self._record_ref.cluster.peek(self.sandbox_id)
        return record is not None and record.alive and not record.expired()

    def set_network(
        self,
        allow_out: list[str] | None = None,
        deny_out: list[str] | None = None,
        **opts: Any,
    ) -> None:
        """Mirrors novita ``Sandbox.set_network``."""
        del opts
        network: dict[str, Any] = {}
        if allow_out is not None:
            network["allow_out"] = list(allow_out)
        if deny_out is not None:
            network["deny_out"] = list(deny_out)
        self._record().network = network

    def update_network(self, network: Mapping[str, Any], **opts: Any) -> None:
        """Mirrors e2b ``Sandbox.update_network`` (replaces the egress rules)."""
        del opts
        self._record().network = {
            key: list(value) for key, value in network.items() if key in ("allow_out", "deny_out")
        }

    def create_snapshot(
        self, name: str | None = None, *, mode: str | None = None, **opts: Any
    ) -> FakeSnapshotInfo:
        """Mirrors e2b ``create_snapshot``; novita calls it without arguments."""
        del name, mode, opts
        return FakeSnapshotInfo(self._record().cluster.snapshot(self.sandbox_id))


class FakeSandboxInfo:
    """Mirrors the SDK ``SandboxInfo`` fields the adapter reads."""

    def __init__(self, sandbox_id: str, metadata: Mapping[str, str], started_at: float) -> None:
        self.sandbox_id = sandbox_id
        self.metadata = dict(metadata)
        self.started_at = started_at


class FakeSandboxPaginator:
    """Mirrors the SDK paginator protocol (``has_next`` / ``next_items``)."""

    def __init__(self, items: list[FakeSandboxInfo]) -> None:
        self._items = items
        self.has_next = bool(items)

    def next_items(self) -> list[FakeSandboxInfo]:
        if not self.has_next:
            raise RuntimeError("no more items")
        items, self._items = self._items, []
        self.has_next = False
        return items


class _ClassMethodVariant:
    """Mirror of the SDKs' ``class_method_variant`` decorator.

    Accessed on the class, it binds the class implementation (whose first
    parameter is the class); accessed on an instance, it delegates to the
    named instance method inherited from the base fake class.
    """

    def __init__(self, class_impl: Callable[..., Any], instance_attr: str) -> None:
        self._class_impl = class_impl
        self._instance_attr = instance_attr

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return _types.MethodType(self._class_impl, objtype)
        return getattr(obj, self._instance_attr)


def make_fake_sandbox_class(cluster: FakeE2BCluster) -> Any:
    """Bind a ``FakeSandbox`` subclass to a cluster for class-level SDK calls.

    The subclass keeps the real method signatures (checked against the vendor
    SDKs by the parity test).
    """

    class ClusterSandbox(FakeSandbox):
        def __init__(self, record: FakeSandboxRecord) -> None:
            super().__init__(record)

        @classmethod
        def create(
            cls,
            template: str | None = None,
            timeout: int | None = None,
            metadata: Mapping[str, str] | None = None,
            envs: Mapping[str, str] | None = None,
            allow_internet_access: bool = True,
            network: Mapping[str, Any] | None = None,
            **opts: Any,
        ) -> ClusterSandbox:
            del envs, opts
            return cls(
                cluster.create(
                    template=template,
                    timeout=timeout,
                    metadata=metadata,
                    allow_internet_access=allow_internet_access,
                    network=network,
                )._record_ref
            )

        @classmethod
        def list(
            cls,
            query: Any = None,
            limit: int | None = None,
            next_token: str | None = None,
            **opts: Any,
        ) -> FakeSandboxPaginator:
            del limit, next_token, opts
            metadata = getattr(query, "metadata", None) if query is not None else None
            infos = [
                FakeSandboxInfo(sandbox_id, record_metadata, started)
                for sandbox_id, record_metadata, started in cluster.list(metadata)
            ]
            return FakeSandboxPaginator(infos)

        def _kill_by_id(cls: Any, sandbox_id: str, **opts: Any) -> bool:
            del cls, opts
            return cluster.kill(str(sandbox_id))

        kill = _ClassMethodVariant(_kill_by_id, "_instance_kill")

    return ClusterSandbox


class FakeSandboxNamespace:
    """The ``.sandbox`` namespace of the fake ``Novita`` facade."""

    def __init__(self, cluster: FakeE2BCluster, facade: FakeNovita) -> None:
        self._cluster = cluster
        self._facade = facade
        self.Sandbox = make_fake_sandbox_class(cluster)

    def create(self, *args: Any, **opts: Any) -> FakeSandbox:
        opts.pop("api_key", None)
        opts.pop("domain", None)
        sandbox: FakeSandbox = self.Sandbox.create(*args, **opts)
        return sandbox


class FakeNovita:
    """Mirrors the ``novita_sandbox.Novita`` facade used by the adapter.

    Records the API key it was constructed with so tests can assert the key
    stays on the control plane and never reaches a sandbox.
    """

    def __init__(self, cluster: FakeE2BCluster, *args: Any, **opts: Any) -> None:
        del args
        self.api_key = opts.get("api_key")
        self.domain = opts.get("domain")
        self.sandbox = FakeSandboxNamespace(cluster, self)


def make_fake_novita_class(cluster: FakeE2BCluster) -> type:
    class ClusterNovita(FakeNovita):
        def __init__(self, *args: Any, **opts: Any) -> None:
            super().__init__(cluster, *args, **opts)

    return ClusterNovita


# ---------------------------------------------------------------------------
# SDK patching (the fakes are installed over the SDK entry points the
# adapter modules imported)
# ---------------------------------------------------------------------------


def patch_adapter_module(adapter_module: Any, cluster: FakeE2BCluster) -> dict[str, Any]:
    """Patch an adapter module's SDK references to the fake cluster.

    ``adapter_module`` is either the ``novita`` or the ``selfhost`` module of
    this package. Returns the original values so callers can restore them
    (the conformance target stops the patches in its cleanup).
    """
    originals: dict[str, Any] = {}
    if hasattr(adapter_module, "Novita"):
        originals["Novita"] = adapter_module.Novita
        adapter_module.Novita = make_fake_novita_class(cluster)
    for name in (
        "Sandbox",
        "CommandExitException",
        "SandboxNotFoundException",
        "RateLimitException",
        "SandboxException",
    ):
        if hasattr(adapter_module, name):
            originals[name] = getattr(adapter_module, name)
    fake_exceptions = {
        "Sandbox": make_fake_sandbox_class(cluster),
        "CommandExitException": FakeCommandExitException,
        "SandboxNotFoundException": FakeSandboxNotFoundException,
        "RateLimitException": FakeRateLimitException,
        "SandboxException": FakeSandboxException,
    }
    for name, value in fake_exceptions.items():
        if name in originals:
            setattr(adapter_module, name, value)
    return originals


def unpatch_adapter_module(adapter_module: Any, originals: Mapping[str, Any]) -> None:
    for name, value in originals.items():
        setattr(adapter_module, name, value)


__all__ = [
    "FakeCommandExitException",
    "FakeCommandHandle",
    "FakeCommandResult",
    "FakeE2BCluster",
    "FakeFileNotFoundException",
    "FakeNovita",
    "FakeRateLimitException",
    "FakeSandbox",
    "FakeSandboxException",
    "FakeSandboxNotFoundException",
    "make_fake_novita_class",
    "make_fake_sandbox_class",
    "patch_adapter_module",
    "unpatch_adapter_module",
]
