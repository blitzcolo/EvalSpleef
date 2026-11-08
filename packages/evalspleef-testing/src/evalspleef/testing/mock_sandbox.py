"""In-memory sandbox provider backed by a real temp directory and real subprocesses.

Each sandbox owns a temp directory that acts as its filesystem root: ``/workspace`` inside the
sandbox maps to ``<tmp>/workspace``. Commands run for real through ``/bin/sh -c`` (str) or as a
direct argv (list), so exit codes, signals and process trees behave exactly like production.
What is simulated is the *vendor*: lifetime is enforced with the injected clock, quota and
failure injection are programmable, and network policies are validated but not enforced.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
import time
import uuid
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxLost,
    SpleefError,
    TransferError,
)
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    OutputCallback,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from .clock import Clock, SystemClock

_SANDBOX_BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

# Cap on in-memory accumulation per exec before truncation kicks in; guards against runaway
# producers while staying far above any realistic test assertion.
_ACCUMULATION_LIMIT = 64 << 20


def default_capabilities() -> SandboxCapabilities:
    """The capability set the mock declares unless overridden in the constructor."""
    return SandboxCapabilities(
        os_families=frozenset({OSFamily.LINUX}),
        arches=frozenset({Arch.X86_64, Arch.ARM64}),
        isolation=Isolation.L1,
        net_modes=frozenset(
            {NetMode.OFF, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR, NetMode.FULL}
        ),
        allowlist_kinds=frozenset({"hostname"}),
        dynamic_network_policy=False,
        streaming=True,
        stdin=True,
        snapshot="none",
        snapshot_fanout=False,
        pause_resume=False,
        gui="none",
        gpu=False,
        port_expose=False,
        host_mounts=False,
        enforces_limits=frozenset({"cpu", "memory"}),
        max_concurrency=8,
        max_lifetime_s=7200,
        max_resources=None,
        typical_start_s=0.1,
        reset_cost_s=0.1,
    )


class MockSandbox(Sandbox):
    """One live double instance: a temp directory plus subprocess execution."""

    def __init__(
        self,
        sandbox_id: str,
        spec: SandboxSpec,
        clock: Clock,
        created_at: float,
        expires_at: float,
    ) -> None:
        self.id = sandbox_id
        self.spec = spec
        self.created_at = created_at
        self.expires_at = expires_at
        self._clock = clock
        self._root = Path(tempfile.mkdtemp(prefix=f"evalspleef-mock-{sandbox_id}-"))
        # A sandbox that is dropped without destroy() must not strand its root in the temp
        # directory; the finalizer also runs at interpreter exit.
        weakref.finalize(self, shutil.rmtree, self._root, ignore_errors=True)
        self._root_resolved = Path(os.path.realpath(self._root))
        self._workspace = self._root / "workspace"
        self._workspace.mkdir()
        self._destroyed = False
        self._lost = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def root(self) -> Path:
        """Host directory backing the sandbox filesystem (test-side inspection)."""
        return self._root

    @property
    def alive(self) -> bool:
        return not self._destroyed

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def lost(self) -> bool:
        return self._lost

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def mark_lost(self) -> None:
        """Simulate the instance vanishing mid-trial (host offline, vendor reclaim)."""
        self._lost = True

    def _ensure_usable(self) -> None:
        if self._destroyed:
            raise SandboxLost(f"sandbox {self.id} was destroyed")
        if self._lost:
            raise SandboxLost(f"sandbox {self.id} is lost")
        if self.expired(self._clock.now()):
            raise SandboxLost(f"sandbox {self.id} exceeded its lifetime")

    async def destroy(self) -> None:
        """Idempotent: destroying a missing instance succeeds."""
        if self._destroyed:
            return
        self._destroyed = True
        shutil.rmtree(self._root, ignore_errors=True)

    async def describe(self) -> dict[str, Any]:
        return {
            "host": "mock",
            "resources": self.spec.resources,
            "template_digest": self.spec.template.digest,
            "root": str(self._root),
        }

    # -- path mapping --------------------------------------------------------

    def _map(self, remote: str) -> Path:
        """Map a sandbox-absolute path onto the temp root, refusing escapes."""
        if not remote.startswith("/"):
            remote = f"/{remote}"
        mapped = Path(os.path.normpath(str(self._root_resolved / remote.lstrip("/"))))
        if os.path.commonpath([str(self._root_resolved), str(mapped)]) != str(self._root_resolved):
            raise TransferError(f"path escapes sandbox root: {remote}")
        return mapped

    def _map_existing(self, remote: str) -> Path:
        """Like ``_map`` but also refuse symlink chains that leave the sandbox root."""
        mapped = self._map(remote)
        resolved = Path(os.path.realpath(mapped))
        if os.path.commonpath([str(self._root_resolved), str(resolved)]) != str(
            self._root_resolved
        ):
            raise TransferError(f"symlink escapes sandbox root: {remote}")
        return mapped

    # -- exec ----------------------------------------------------------------

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        self._ensure_usable()
        del user  # the double runs commands as the current host user; see README
        argv = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else [str(a) for a in cmd]
        cwd_path = self._map(cwd or "/workspace")
        if not cwd_path.is_dir():
            raise TransferError(f"cwd does not exist in sandbox: {cwd}")
        full_env = dict(_SANDBOX_BASE_ENV)
        full_env.update(self.spec.env)
        full_env.update(env or {})

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        accumulated = 0
        truncated = False

        def _collect(chunks: list[bytes], data: bytes) -> None:
            nonlocal accumulated, truncated
            if accumulated < _ACCUMULATION_LIMIT:
                chunks.append(data)
                accumulated += len(data)
            else:
                truncated = True

        async def _pump(name: str, chunks: list[bytes]) -> None:
            assert proc.stdout is not None
            stream = proc.stdout if name == "stdout" else proc.stderr
            assert stream is not None
            while True:
                data = await stream.read(65536)
                if not data:
                    return
                _collect(chunks, data)
                if on_output is not None:
                    on_output(name, data)  # type: ignore[arg-type]

        async def _feed_stdin() -> None:
            if stdin is None:
                return
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the process exited before consuming stdin

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd_path),
            env=full_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        pumps = [
            asyncio.create_task(_pump("stdout", stdout_chunks)),
            asyncio.create_task(_pump("stderr", stderr_chunks)),
            asyncio.create_task(_feed_stdin()),
        ]
        timed_out = False
        try:
            if timeout_s is not None:
                await asyncio.wait_for(
                    asyncio.gather(pumps[0], pumps[1], pumps[2]), timeout=timeout_s
                )
            else:
                await asyncio.gather(*pumps)
        except TimeoutError:
            timed_out = True
            # Kill the whole process group so background children die with the shell.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await asyncio.gather(*pumps, return_exceptions=True)
        finally:
            for task in pumps:
                task.cancel()
        exit_code = await proc.wait()
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        duration = time.monotonic() - start

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)
        if len(stdout) + len(stderr) > max_output_bytes:
            truncated = True
            if stdout and stderr:
                per_stream = max_output_bytes // 2
                stdout = _head_tail(stdout, per_stream)
                stderr = _head_tail(stderr, per_stream)
            else:
                stdout = _head_tail(stdout, max_output_bytes)
                stderr = _head_tail(stderr, max_output_bytes)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=duration,
            truncated=truncated,
        )

    # -- upload / download ----------------------------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._ensure_usable()
        local = Path(local)
        if not local.exists() and not local.is_symlink():
            raise TransferError(f"local path does not exist: {local}")
        if local.is_symlink() and not local.resolve().is_relative_to(local.parent.resolve()):
            raise TransferError(f"refusing symlink that escapes the uploaded tree: {local}")
        dest = self._map(remote)
        if local.is_symlink():
            # A host-side symlink into the sandbox root is meaningless; refuse ambiguous ones.
            target = local.resolve()
            if os.path.commonpath([str(self._root_resolved), str(target)]) != str(
                self._root_resolved
            ):
                raise TransferError(f"refusing symlink that escapes the sandbox: {local}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            os.symlink(os.readlink(local), dest)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(local, dest, symlinks=True, copy_function=shutil.copy2)
        else:
            shutil.copy2(local, dest)

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._ensure_usable()
        src = self._map_existing(remote)
        if not src.exists() and not src.is_symlink():
            raise TransferError(f"remote path does not exist: {remote}")
        local = Path(local)
        if src.is_symlink():
            local.parent.mkdir(parents=True, exist_ok=True)
            if local.exists() or local.is_symlink():
                local.unlink()
            os.symlink(os.readlink(src), local)
            return
        if src.is_dir():
            total = _tree_size(src)
            if max_bytes is not None and total > max_bytes:
                raise TransferError(f"download exceeds max_bytes: {total} > {max_bytes}")
            if local.exists():
                shutil.rmtree(local)
            _copy_tree_sanitized(src, local)
        else:
            if max_bytes is not None and src.stat().st_size > max_bytes:
                raise TransferError(f"download exceeds max_bytes: {src.stat().st_size}")
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, local)


def _head_tail(data: bytes, budget: int) -> bytes:
    """Keep the head and the tail of an over-long stream (PRD truncation convention)."""
    if len(data) <= budget:
        return data
    half = budget // 2
    return data[:half] + data[-half:]


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def _copy_tree_sanitized(src: Path, dest: Path) -> None:
    """Copy a directory, refusing symlinks that point outside ``src`` (artifact sanitizing)."""
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


class MockSandboxProvider(SandboxProvider):
    """Programmable sandbox provider: capability checks, quota, failure injection."""

    name = "mock-sandbox"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
        capabilities: SandboxCapabilities | None = None,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._clock: Clock = clock or SystemClock()
        self._capabilities = capabilities or default_capabilities()
        self._sandboxes: dict[str, MockSandbox] = {}
        self._fail_next: list[type[SpleefError] | SpleefError] = []
        self._quota: int | None = None

    # -- test-side controls ---------------------------------------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    def lose(self, sandbox_id: str) -> None:
        """Simulate the given sandbox vanishing; the next use raises ``SandboxLost``."""
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise KeyError(f"unknown sandbox: {sandbox_id}")
        sandbox.mark_lost()

    def set_quota(self, n: int) -> None:
        """Cap the number of live sandboxes; excess creates raise ``QuotaExceeded``."""
        self._quota = n

    def created_sandboxes(self) -> list[MockSandbox]:
        """All sandboxes ever created, including destroyed ones (test assertions)."""
        return list(self._sandboxes.values())

    # -- SandboxProvider -------------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        return self._capabilities

    def isolation_profile(self) -> IsolationProfile:
        def waived(how: str, probe: str | None = None) -> InvariantEntry:
            return InvariantEntry(how=how, probe=probe, waived=True, reason="test double")

        return IsolationProfile(
            provider=self.name,
            declared_isolation=self._capabilities.isolation,
            invariants={
                "I1": InvariantEntry(
                    how="every trial gets a fresh temp directory, destroyed on teardown",
                    probe="list(labels) == 1 per lease",
                ),
                "I2": waived("not applicable to a local test double"),
                "I3": waived(
                    "each sandbox owns a private host temp directory",
                    "two sandboxes do not share a root path",
                ),
                "I4": waived("not applicable to a local test double"),
                "I5": waived("not applicable to a local test double"),
                "I6": waived("not applicable to a local test double"),
                "I7": waived("not applicable to a local test double"),
                "I8": waived("not applicable to a local test double"),
                "I9": waived(
                    "no control-plane credentials exist in the double; env merge is explicit",
                    "env contains only spec.env and exec env",
                ),
                "I10": waived("not applicable to a local test double"),
                "I11": waived(
                    "download refuses symlinks escaping the sandbox root",
                    "export of an escaping symlink raises TransferError",
                ),
                "I12": waived("not applicable to a local test double"),
                "I13": waived("not applicable to a local test double"),
                "I14": waived(
                    "instances are never pooled or reused; destroy removes the directory",
                    "destroy is idempotent and wipes the root",
                ),
                "I15": waived("not applicable to a local test double"),
                "I16": waived("not applicable to a local test double"),
            },
        )

    async def preflight(self) -> dict[str, Any]:
        live = sum(1 for s in self._sandboxes.values() if s.alive)
        return {
            "healthy": True,
            "instance": self.instance,
            "live_sandboxes": live,
            "quota": self._quota,
        }

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            failure = self._fail_next.pop(0)
            raise failure
        cap = self._capabilities
        if spec.os not in cap.os_families:
            raise CapabilityNotSupported(f"os not supported: {spec.os}")
        if spec.arch not in cap.arches:
            raise CapabilityNotSupported(f"arch not supported: {spec.arch}")
        if spec.network.mode not in cap.net_modes:
            raise PolicyNotEnforceable(f"network mode not enforceable: {spec.network.mode}")
        if spec.network.allow_cidrs and "cidr" not in cap.allowlist_kinds:
            raise PolicyNotEnforceable("cidr allowlist not enforceable")
        if (
            spec.network.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR)
            and spec.network.allow_hosts
            and "hostname" not in cap.allowlist_kinds
            and spec.network.via_proxy is None
        ):
            raise PolicyNotEnforceable("hostname allowlist not enforceable without a proxy")
        if spec.gui and cap.gui == "none":
            raise CapabilityNotSupported("gui not supported")
        if cap.max_lifetime_s is not None and spec.max_lifetime_s > cap.max_lifetime_s:
            raise CapabilityNotSupported(
                f"max_lifetime_s {spec.max_lifetime_s} exceeds capability {cap.max_lifetime_s}"
            )
        limits = cap.max_resources
        if limits is not None and (
            spec.resources.cpus > limits.cpus
            or spec.resources.memory_mb > limits.memory_mb
            or spec.resources.disk_mb > limits.disk_mb
        ):
            raise CapabilityNotSupported("requested resources exceed provider maximum")
        if self._quota is not None:
            live = sum(1 for s in self._sandboxes.values() if s.alive)
            if live >= self._quota:
                raise QuotaExceeded(f"sandbox quota exhausted: {live}/{self._quota}")

        sandbox_id = f"sb-{uuid.uuid4().hex[:12]}"
        created_at = self._clock.now()
        sandbox = MockSandbox(
            sandbox_id, spec, self._clock, created_at, created_at + spec.max_lifetime_s
        )
        self._sandboxes[sandbox_id] = sandbox
        return sandbox

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "labels": dict(s.spec.labels),
                "role": s.spec.role,
                "trial_id": s.spec.trial_id,
                "created_at": s.created_at,
                "expires_at": s.expires_at,
            }
            for s in self._sandboxes.values()
            if s.alive and _labels_match(s.spec.labels, labels)
        ]

    async def sweep_expired(self) -> int:
        """The double's own TTL: destroy every instance past its lifetime, like a vendor timeout."""
        now = self._clock.now()
        expired = [s for s in self._sandboxes.values() if s.alive and s.expired(now)]
        for sandbox in expired:
            await sandbox.destroy()
        return len(expired)

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Destroy orphans matching ``labels`` that are older than ``older_than_s`` or expired."""
        now = self._clock.now()
        destroyed = 0
        for sandbox in list(self._sandboxes.values()):
            if not sandbox.alive or not _labels_match(sandbox.spec.labels, labels):
                continue
            if sandbox.lost or now - sandbox.created_at >= older_than_s or sandbox.expired(now):
                await sandbox.destroy()
                destroyed += 1
        return destroyed


def _labels_match(spec_labels: Mapping[str, str], query: Mapping[str, str]) -> bool:
    return all(spec_labels.get(key) == value for key, value in query.items())
