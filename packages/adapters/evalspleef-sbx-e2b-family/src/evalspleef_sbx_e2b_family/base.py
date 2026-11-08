"""Shared logic for E2B-family sandbox providers (TECH-DESIGN section 7.4.1).

The Novita Agent Sandbox SDK (``novita-sandbox`` 2.x) and the E2B SDK share the
same envd-shaped surface: a sandbox handle with ``commands.run`` /
``files.write`` / ``files.read`` / ``kill``, created with ``template``,
``timeout``, ``metadata``, ``allow_internet_access`` and ``network`` options.
This module implements the EvalSpleef sandbox SPI once, against a small duck-typed
SDK protocol, so the vendor-specific packages only supply SDK entry points,
error types and capability numbers.

Shared behaviour implemented here:

* instance ``timeout = spec.max_lifetime_s + 300`` (the SDK default of 5 minutes
  would kill trials early; FR-SBX-04 requires an explicit timeout);
* ``exec`` mapped to ``commands.run`` with cwd/env/user, streaming stdout/stderr
  callbacks, oversized-output truncation and process-group kill on timeout
  (``kill -- -<pgid>`` then ``exit_code=124``, per the SPI);
* environment sanitization: commands run through ``env -i`` with a whitelist,
  because E2B-family platforms inject identifying variables into the template
  environment (invariant I8); the agent user's login profile is cleaned at
  template build time (see :mod:`evalspleef_sbx_e2b_family.template_builder`);
* ``upload``/``download`` mapped to ``files.write``/``files.read``; directories
  travel as tar archives uploaded/downloaded through the files API and unpacked
  inside the sandbox; downloads refuse symlinks that resolve outside
  ``/workspace``;
* ``destroy`` mapped to ``kill`` (idempotent);
* instance ``metadata`` written as labels so ``list``/``reap`` can find orphans;
* network policy mapping: ``off`` -> ``allow_internet_access=False``,
  allowlist modes -> ``network.allow_out`` (hostnames, wildcards, IPs, CIDRs),
  and ``via_proxy`` restricts outbound traffic to the proxy address only;
* quota enforcement: concurrency beyond the configured limit raises
  ``QuotaExceeded``; resource/lifetime requests beyond the declared capability
  ceiling raise ``CapabilityNotSupported``.

All vendor SDK calls are synchronous, so they run in worker threads via
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import posixpath
import re
import shlex
import tarfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
    TransferError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    TIMEOUT_EXIT_CODE,
    ExecResult,
    NetworkPolicy,
    OutputCallback,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)

if TYPE_CHECKING:
    from evalspleef.spi.secrets import SecretResolver

#: Extra lifetime headroom added to ``spec.max_lifetime_s`` for the vendor-side
#: TTL, so the sandbox never dies mid-teardown bookkeeping (TECH-DESIGN 7.4.1).
LIFETIME_GRACE_S = 300

#: The fixed workspace path of the sandbox SPI (TECH-DESIGN 7.1).
WORKSPACE = "/workspace"

#: Minimal PATH for sanitized commands; bash resolves the rest from the image.
_SANITIZED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Environment variable names accepted from callers (validation at the boundary).
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How long to wait for a timed-out command to die after the process-group kill.
_KILL_GRACE_S = 15.0

#: Generous bound for the file-transfer helper commands inside the sandbox.
_TRANSFER_TIMEOUT_S = 600.0


# ---------------------------------------------------------------------------
# Duck-typed SDK protocols (implemented by the real SDKs and by fakes/)
# ---------------------------------------------------------------------------


class SdkSandboxHandle(Protocol):
    """Duck-typed vendor sandbox object (``novita_sandbox.Sandbox`` / ``e2b.Sandbox``).

    ``commands`` and ``files`` are the vendors' overloaded command and filesystem
    modules; their exact call shapes are pinned against the installed SDKs by the
    fake-signature parity test instead of being re-declared as protocols here.
    Vendor-specific operations (network update, snapshots) go through provider hooks.
    """

    @property
    def sandbox_id(self) -> str: ...

    @property
    def commands(self) -> Any: ...

    @property
    def files(self) -> Any: ...

    def kill(self) -> bool: ...

    def is_running(self) -> bool: ...


class _Outcome:
    """Normalized command outcome independent of vendor result types."""

    __slots__ = ("exit_code", "stderr", "stdout")

    def __init__(self, *, stdout: bytes, stderr: bytes, exit_code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _Output:
    __slots__ = ("data", "truncated")

    def __init__(self, *, data: bytes, truncated: bool) -> None:
        self.data = data
        self.truncated = truncated


class E2BFamilyProvider(SandboxProvider):
    """Base class for E2B-family sandbox providers.

    Subclasses supply the vendor SDK entry points (create/list/kill/snapshot),
    the vendor exception types used for error mapping, and the capability
    ceiling. Everything else — validation, env sanitization, exec/upload/download
    semantics, timeouts, labels — lives here.
    """

    name = "e2b-family"

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        super().__init__(instance, config, secrets)
        self._active: set[E2BFamilySandbox] = set()
        self._creating = 0
        self._create_lock = asyncio.Lock()

    # -- clocks (overridden by test doubles) ---------------------------------

    def _now(self) -> float:
        """Wall clock used for reap age accounting."""
        return time.time()

    def _monotonic(self) -> float:
        """Monotonic clock used for exec duration accounting."""
        return time.monotonic()

    # -- vendor SDK entry points (implemented by subclasses) ------------------

    def _create_handle(
        self,
        *,
        template: str,
        timeout: int,
        metadata: Mapping[str, str],
        allow_internet_access: bool,
        network: Mapping[str, Any] | None,
    ) -> SdkSandboxHandle:
        """Create a vendor sandbox. Vendor errors are mapped by the caller."""
        raise NotImplementedError

    def _list_handles(
        self, labels: Mapping[str, str]
    ) -> list[tuple[str, Mapping[str, str], float | None]]:
        """List live sandboxes as ``(sandbox_id, metadata, started_at_epoch)``."""
        raise NotImplementedError

    def _kill_handle(self, sandbox_id: str) -> bool:
        """Kill a vendor sandbox by id; a missing instance is not an error."""
        raise NotImplementedError

    def _create_snapshot(self, handle: Any, name: str) -> str:
        """Snapshot the sandbox and return the snapshot id."""
        raise NotImplementedError

    def _set_network(
        self, handle: Any, allow_out: list[str] | None, deny_out: list[str] | None
    ) -> None:
        """Replace the egress rules of a running sandbox (vendor specific call)."""
        raise NotImplementedError

    # -- vendor error types (implemented by subclasses) -----------------------

    def _command_exit_exception(self) -> type[Exception]:
        """The vendor exception raised by ``CommandHandle.wait`` on non-zero exit.

        It carries ``stdout``, ``stderr`` and ``exit_code`` attributes.
        """
        raise NotImplementedError

    def _not_found_exceptions(self) -> tuple[type[Exception], ...]:
        """Vendor exceptions meaning 'this sandbox no longer exists'."""
        raise NotImplementedError

    def _rate_limit_exceptions(self) -> tuple[type[Exception], ...]:
        """Vendor exceptions meaning 'vendor quota/rate limit hit'."""
        raise NotImplementedError

    def _vendor_exceptions(self) -> tuple[type[Exception], ...]:
        """Base class of the vendor SDK error hierarchy."""
        raise NotImplementedError

    # -- capability ceiling and profile (implemented by subclasses) -----------

    def _capability_defaults(self) -> SandboxCapabilities:
        """Capability ceiling before config limits are applied."""
        raise NotImplementedError

    def _profile(self) -> IsolationProfile:
        raise NotImplementedError

    def _template_kind(self) -> str:
        raise NotImplementedError

    def _api_key_ref(self) -> str | None:
        """SecretRef used to authenticate against the vendor control API."""
        ref = self.config.get("api_key_ref")
        return str(ref) if ref else None

    # -- shared helpers --------------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        caps = self._capability_defaults()
        limits = self.config.get("limits") or {}
        max_concurrency = limits.get("max_concurrency", caps.max_concurrency)
        max_lifetime_s = limits.get("max_lifetime_s", caps.max_lifetime_s)
        max_resources = caps.max_resources
        override = limits.get("max_resources")
        if override and max_resources is not None:
            max_resources = type(max_resources)(
                cpus=min(max_resources.cpus, float(override.get("cpus", max_resources.cpus))),
                memory_mb=min(
                    max_resources.memory_mb,
                    int(override.get("memory_mb", max_resources.memory_mb)),
                ),
                disk_mb=min(
                    max_resources.disk_mb, int(override.get("disk_mb", max_resources.disk_mb))
                ),
                gpus=max_resources.gpus,
                gpu_type=max_resources.gpu_type,
            )
        return SandboxCapabilities(
            os_families=caps.os_families,
            arches=caps.arches,
            isolation=caps.isolation,
            net_modes=caps.net_modes,
            allowlist_kinds=caps.allowlist_kinds,
            dynamic_network_policy=caps.dynamic_network_policy,
            streaming=caps.streaming,
            stdin=caps.stdin,
            snapshot=caps.snapshot,
            snapshot_fanout=caps.snapshot_fanout,
            pause_resume=caps.pause_resume,
            gui=caps.gui,
            gpu=caps.gpu,
            port_expose=caps.port_expose,
            host_mounts=caps.host_mounts,
            enforces_limits=caps.enforces_limits,
            max_concurrency=int(max_concurrency) if max_concurrency is not None else None,
            max_lifetime_s=int(max_lifetime_s) if max_lifetime_s is not None else None,
            max_resources=max_resources,
            typical_start_s=caps.typical_start_s,
            reset_cost_s=caps.reset_cost_s,
        )

    def isolation_profile(self) -> IsolationProfile:
        return self._profile()

    async def preflight(self) -> dict[str, Any]:
        """Check that credentials resolve and report the configured ceiling.

        Vendor-side quota queries need network access and are best-effort; the
        SPI only guarantees that an unusable configuration is detected here.
        """
        ref = self._api_key_ref()
        ok = True
        error: str | None = None
        if ref is not None:
            try:
                self.secrets.resolve(ref)
            except Exception as exc:  # resolvers raise their own error types
                ok = False
                error = f"cannot resolve api_key_ref {ref!r}: {exc}"
        caps = self.capabilities()
        return {
            "ok": ok,
            "error": error,
            "instance": self.instance,
            "adapter": self.name,
            "credentials_resolvable": ok,
            "max_concurrency": caps.max_concurrency,
            "max_lifetime_s": caps.max_lifetime_s,
            "max_resources": caps.max_resources,
        }

    # -- network policy mapping -------------------------------------------------

    @staticmethod
    def _proxy_host(via_proxy: str) -> str:
        """Extract a host (no port, no scheme) from a proxy address."""
        value = via_proxy
        for prefix in ("http://", "https://", "socks5://"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break
        return value.split("/", 1)[0].split(":", 1)[0]

    def _network_options(self, policy: NetworkPolicy) -> tuple[bool, dict[str, Any] | None]:
        """Map an SPI network policy to SDK create options.

        Returns ``(allow_internet_access, network_opts)`` where ``network_opts``
        is None when the vendor default (allow everything) is correct.
        """
        if policy.mode is NetMode.OFF:
            return False, None
        if (
            policy.mode is NetMode.FULL
            and not policy.allow_hosts
            and not policy.allow_cidrs
            and policy.via_proxy is None
        ):
            return True, None
        if policy.via_proxy is not None:
            # ADR-v3-07: the sandbox may reach only the shared egress proxy;
            # the proxy enforces the domain allowlist and keeps URL logs (I16).
            return True, {"allow_out": [self._proxy_host(policy.via_proxy)]}
        allow_out = [*policy.allow_hosts, *policy.allow_cidrs]
        if not allow_out:
            raise PolicyNotEnforceable(
                f"network mode {policy.mode.value!r} needs an allowlist or via_proxy",
                detail={"mode": policy.mode.value},
            )
        return True, {"allow_out": allow_out}

    # -- spec validation ---------------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> SandboxCapabilities:
        caps = self.capabilities()
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(f"os {spec.os.value!r} not supported by {self.name}")
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(f"arch {spec.arch.value!r} not supported by {self.name}")
        if spec.gui and caps.gui == "none":
            raise CapabilityNotSupported(f"{self.name} does not support GUI sandboxes")
        if spec.resources.gpus and not caps.gpu:
            raise CapabilityNotSupported(f"{self.name} does not support GPU sandboxes")
        if caps.max_lifetime_s is not None and spec.max_lifetime_s > caps.max_lifetime_s:
            raise CapabilityNotSupported(
                f"max_lifetime_s {spec.max_lifetime_s} exceeds the {caps.max_lifetime_s}s ceiling"
            )
        if caps.max_resources is not None:
            max_r = caps.max_resources
            if (
                spec.resources.cpus > max_r.cpus
                or spec.resources.memory_mb > max_r.memory_mb
                or spec.resources.disk_mb > max_r.disk_mb
            ):
                raise CapabilityNotSupported(
                    "requested resources exceed the capability ceiling: "
                    f"{spec.resources!r} > {max_r!r}"
                )
        if spec.template.kind != self._template_kind():
            raise CapabilityNotSupported(
                f"template kind {spec.template.kind!r} does not match {self.name!r} "
                f"(expected {self._template_kind()!r})"
            )
        if spec.network.mode not in caps.net_modes:
            raise PolicyNotEnforceable(
                f"network mode {spec.network.mode.value!r} not supported by {self.name}",
                detail={"mode": spec.network.mode.value},
            )
        return caps

    # -- lifecycle ---------------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await self._create_checked(spec, template=spec.template.ref)

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        if self.capabilities().snapshot == "none":
            raise CapabilityNotSupported(f"{self.name} does not support snapshots")
        return await self._create_checked(spec, template=snapshot_id)

    async def _create_checked(self, spec: SandboxSpec, *, template: str) -> Sandbox:
        caps = self._validate_spec(spec)
        await self._acquire_slot(caps)
        handle: SdkSandboxHandle | None = None
        try:
            allow_internet, network = self._network_options(spec.network)
            timeout = spec.max_lifetime_s + LIFETIME_GRACE_S
            metadata = {str(k): str(v) for k, v in spec.labels.items()}
            try:
                handle = await asyncio.to_thread(
                    self._create_handle,
                    template=template,
                    timeout=timeout,
                    metadata=metadata,
                    allow_internet_access=allow_internet,
                    network=network,
                )
            except self._rate_limit_exceptions() as exc:
                raise QuotaExceeded(f"{self.name} rate limit during create: {exc}") from exc
            except self._vendor_exceptions() as exc:
                raise SandboxCreateError(f"{self.name} sandbox creation failed: {exc}") from exc
        except BaseException:
            self._release_slot()
            raise
        sandbox = self._wrap_handle(handle, spec)
        self._active.add(sandbox)
        self._release_slot()
        return sandbox

    async def _acquire_slot(self, caps: SandboxCapabilities) -> None:
        """Reserve a slot for one create; creates still in flight count against the limit."""
        async with self._create_lock:
            if (
                caps.max_concurrency is not None
                and len(self._active) + self._creating >= caps.max_concurrency
            ):
                raise QuotaExceeded(
                    f"{self.name} instance {self.instance!r} is at its concurrency limit "
                    f"({caps.max_concurrency})",
                    detail={"max_concurrency": caps.max_concurrency},
                )
            self._creating += 1

    def _release_slot(self) -> None:
        """End a reservation: the create failed, or its sandbox is now counted as active."""
        self._creating -= 1

    def _wrap_handle(self, handle: SdkSandboxHandle, spec: SandboxSpec) -> E2BFamilySandbox:
        return E2BFamilySandbox(self, handle, spec)

    def _sandbox_destroyed(self, sandbox: E2BFamilySandbox) -> None:
        self._active.discard(sandbox)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        wanted = {str(k): str(v) for k, v in labels.items()}

        def _query() -> list[dict[str, Any]]:
            entries = []
            for sandbox_id, metadata, started_at in self._list_handles(wanted):
                if all(metadata.get(key) == value for key, value in wanted.items()):
                    entries.append(
                        {"id": sandbox_id, "metadata": dict(metadata), "started_at": started_at}
                    )
            return entries

        return await asyncio.to_thread(_query)

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        now = self._now()
        entries = await self.list(labels)
        reaped = 0
        for entry in entries:
            started_at = entry.get("started_at")
            if started_at is not None and now - float(started_at) < older_than_s:
                continue
            if await asyncio.to_thread(self._kill_handle, str(entry["id"])):
                reaped += 1
        return reaped


class E2BFamilySandbox(Sandbox):
    """One live E2B-family sandbox instance backed by a vendor SDK handle."""

    def __init__(
        self,
        provider: E2BFamilyProvider,
        handle: SdkSandboxHandle,
        spec: SandboxSpec,
    ) -> None:
        self._provider = provider
        self._handle = handle
        self.spec = spec
        self.id = str(handle.sandbox_id)
        self._destroyed = False

    # -- exec --------------------------------------------------------------------

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
        started = self._provider._monotonic()
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def on_stdout(text: str) -> None:
            data = text.encode("utf-8", errors="replace")
            stdout_chunks.append(data)
            if on_output is not None:
                on_output("stdout", data)

        def on_stderr(text: str) -> None:
            data = text.encode("utf-8", errors="replace")
            stderr_chunks.append(data)
            if on_output is not None:
                on_output("stderr", data)

        wrapped = self._wrap_command(cmd, env, user or self.spec.run_as_user)
        exit_exception = self._provider._command_exit_exception()
        # The SPI fixes paths relative to the workspace; the vendor default is the
        # user's home, where a live Novita run created and scanned the wrong files.
        run_cwd = posixpath.join(WORKSPACE, cwd) if cwd else WORKSPACE
        try:
            # Positional arguments mirror `SdkCommands.run`; the SDK runs
            # `/bin/bash -l -c <wrapped>`, and the wrapper performs the
            # env sanitization, so no caller env reaches the vendor `envs` slot.
            command_handle = await asyncio.to_thread(
                self._handle.commands.run,
                wrapped,
                True,  # background -> returns a handle with pid/wait/kill
                None,
                user,
                run_cwd,
                None,
                None,
                stdin is not None,
                0,  # unlimited command connection; the adapter enforces timeout_s
                None,
            )
        except self._provider._not_found_exceptions() as exc:
            raise SandboxLost(f"{self.id}: sandbox vanished before exec: {exc}") from exc

        if stdin is not None:
            await asyncio.to_thread(
                self._handle.commands.send_stdin,
                command_handle.pid,
                stdin.decode("utf-8", errors="replace"),
            )

        async def _wait() -> Any:
            def _collect() -> Any:
                try:
                    return command_handle.wait(on_stdout=on_stdout, on_stderr=on_stderr)
                except exit_exception as exc:
                    return exc

            return await asyncio.to_thread(_collect)

        wait_task = asyncio.ensure_future(_wait())
        timed_out = False
        outcome: Any = None
        try:
            if timeout_s is None:
                outcome = await wait_task
            else:
                done, _pending = await asyncio.wait({wait_task}, timeout=timeout_s)
                if done:
                    outcome = wait_task.result()
                else:
                    timed_out = True
                    await self._kill_process_group(command_handle.pid)
                    try:
                        outcome = await asyncio.wait_for(asyncio.shield(wait_task), _KILL_GRACE_S)
                    except TimeoutError:
                        outcome = None
        finally:
            if not wait_task.done():
                wait_task.cancel()
        duration_s = self._provider._monotonic() - started
        if outcome is not None:
            result = self._result_from_outcome(outcome)
            stdout = self._finish_output([result.stdout], max_output_bytes)
            stderr = self._finish_output([result.stderr], max_output_bytes)
            exit_code = result.exit_code
        else:
            # The kill grace expired; report whatever the callbacks collected.
            stdout = self._finish_output(stdout_chunks, max_output_bytes)
            stderr = self._finish_output(stderr_chunks, max_output_bytes)
            exit_code = TIMEOUT_EXIT_CODE
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.data,
            stderr=stderr.data,
            timed_out=timed_out,
            duration_s=duration_s,
            truncated=stdout.truncated or stderr.truncated,
        )

    def _result_from_outcome(self, outcome: Any) -> _Outcome:
        """Normalize a vendor result or exit exception to an outcome."""
        stdout = str(getattr(outcome, "stdout", "")).encode("utf-8", errors="replace")
        stderr = str(getattr(outcome, "stderr", "")).encode("utf-8", errors="replace")
        return _Outcome(stdout=stdout, stderr=stderr, exit_code=int(outcome.exit_code))

    async def _kill_process_group(self, pid: int) -> None:
        """Kill the whole in-sandbox process group of a timed-out command."""
        snippet = (
            f"pgid=$(ps -o pgid= -p {int(pid)} 2>/dev/null | tr -d ' ');"
            ' if [ -n "$pgid" ]; then kill -KILL -"$pgid" 2>/dev/null || true; fi; exit 0'
        )
        # Best effort: the vendor TTL bounds the damage either way.
        with contextlib.suppress(SpleefError):
            await self.exec(snippet, timeout_s=_KILL_GRACE_S)

    def _wrap_command(self, cmd: str | list[str], env: Mapping[str, str] | None, user: str) -> str:
        """Build the sanitized command string handed to ``commands.run``.

        The platform injects identifying variables into the template environment
        (E2B-family images export e.g. ``E2B_SANDBOX_ID``); running through
        ``env -i`` with a whitelist keeps them out of agent processes
        (invariant I8). The agent user's login profile is cleaned when the
        template is built, so the inner login shell adds nothing back.
        """
        inner = shlex.join([str(part) for part in cmd]) if isinstance(cmd, list) else cmd
        merged = dict(self.spec.env)
        merged.update(env or {})
        assignments = ""
        for key, value in merged.items():
            if not _ENV_NAME.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            assignments += f" {key}={shlex.quote(str(value))}"
        return (
            f"env -i{assignments} PATH={_SANITIZED_PATH} TERM=dumb HOME=/home/{user}"
            f" /bin/bash -lc {shlex.quote(inner)}"
        )

    @staticmethod
    def _finish_output(chunks: list[bytes], budget: int) -> _Output:
        data = b"".join(chunks)
        if len(data) <= budget:
            return _Output(data=data, truncated=False)
        half = budget // 2
        return _Output(data=data[:half] + data[-half:], truncated=True)

    # -- file transfer --------------------------------------------------------------

    async def upload(self, local: Path, remote: str) -> None:
        self._ensure_usable()
        remote_path = self._workspace_path(remote, what="upload")
        local = Path(local)
        if not local.exists():
            raise TransferError(f"local path does not exist: {local}")
        if local.is_dir():
            archive = _tar_tree(local)
            tmp_name = f".spleef-upload-{uuid.uuid4().hex}.tar"
            tmp_remote = str(PurePosixPath(WORKSPACE) / tmp_name)
            await asyncio.to_thread(self._handle.files.write, tmp_remote, archive)
            target_rel = self._rel_to_workspace(remote_path)
            unpack = (
                f"mkdir -p {shlex.quote(target_rel)} && "
                f"tar -xf {shlex.quote(tmp_name)} -C {shlex.quote(target_rel)} && "
                f"rm -f {shlex.quote(tmp_name)}"
            )
            await self._transfer_exec(unpack)
        else:
            await asyncio.to_thread(self._handle.files.write, str(remote_path), local.read_bytes())
            rel = self._rel_to_workspace(remote_path)
            mode = local.stat().st_mode & 0o7777
            await self._transfer_exec(f"chmod {mode:o} {shlex.quote(rel)}")

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._ensure_usable()
        remote_path = self._workspace_path(remote, what="download")
        inside = await self._resolve_inside_workspace(remote_path)
        if not inside:
            raise TransferError(f"remote path escapes the sandbox workspace: {remote}")
        rel = self._rel_to_workspace(remote_path)
        probe = await self._transfer_exec(
            f"if [ -d {shlex.quote(rel)} ]; then echo dir; "
            f"elif [ -f {shlex.quote(rel)} ]; then echo file; else exit 3; fi"
        )
        kind = probe.stdout.decode("utf-8", errors="replace").strip()
        local = Path(local)
        if kind == "file":
            data = await asyncio.to_thread(self._handle.files.read, str(remote_path), "bytes")
            payload = bytes(data)
            if max_bytes is not None and len(payload) > max_bytes:
                raise TransferError(
                    f"remote file is {len(payload)} bytes, exceeding max_bytes={max_bytes}"
                )
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(payload)
        elif kind == "dir":
            tmp_name = f".spleef-download-{uuid.uuid4().hex}.tar"
            await self._transfer_exec(f"tar -cf {shlex.quote(tmp_name)} -C {shlex.quote(rel)} .")
            data = await asyncio.to_thread(
                self._handle.files.read, str(PurePosixPath(WORKSPACE) / tmp_name), "bytes"
            )
            await self._transfer_exec(f"rm -f {shlex.quote(tmp_name)}")
            payload = bytes(data)
            if max_bytes is not None and len(payload) > max_bytes:
                raise TransferError(
                    f"remote directory archive is {len(payload)} bytes, exceeding {max_bytes}"
                )
            local.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
                tar.extractall(path=local, filter="data")
        else:
            raise TransferError(f"remote path is neither a file nor a directory: {remote}")

    async def _transfer_exec(self, cmd: str) -> ExecResult:
        result = await self.exec(cmd, cwd=WORKSPACE, timeout_s=_TRANSFER_TIMEOUT_S)
        if result.timed_out or result.exit_code != 0:
            raise TransferError(
                f"in-sandbox transfer command failed (exit {result.exit_code}): {result.stderr!r}"
            )
        return result

    async def _resolve_inside_workspace(self, remote_path: PurePosixPath) -> bool:
        """Follow symlinks and verify the target stays inside /workspace."""
        rel = self._rel_to_workspace(remote_path)
        snippet = (
            f"_p={shlex.quote(rel)}\n"
            'while [ -L "$_p" ]; do\n'
            '  _t=$(readlink "$_p")\n'
            '  case "$_t" in\n'
            "    /*) echo OUTSIDE; exit 0;;\n"
            '    *) _p="$(dirname "$_p")/$_t";;\n'
            "  esac\n"
            "done\n"
            '_d="$(dirname "$_p")"\n'
            '_r="$(CDPATH= cd "$_d" 2>/dev/null && pwd -P)" || { echo OUTSIDE; exit 0; }\n'
            '_w="$(pwd -P)"\n'
            'case "$_r" in\n'
            '  "$_w"|"$_w"/*) echo INSIDE;;\n'
            "  *) echo OUTSIDE;;\n"
            "esac\n"
        )
        result = await self._transfer_exec(snippet)
        return result.stdout.decode("utf-8", errors="replace").strip() == "INSIDE"

    @staticmethod
    def _workspace_path(remote: str, *, what: str) -> PurePosixPath:
        if not remote.startswith("/"):
            raise TransferError(f"{what} path must be absolute, got {remote!r}")
        path = PurePosixPath(remote)
        if path != PurePosixPath(WORKSPACE) and PurePosixPath(WORKSPACE) not in path.parents:
            raise TransferError(f"{what} path must live under {WORKSPACE}: {remote!r}")
        return path

    @staticmethod
    def _rel_to_workspace(path: PurePosixPath) -> str:
        if path == PurePosixPath(WORKSPACE):
            return "."
        return str(path.relative_to(WORKSPACE))

    # -- capabilities ------------------------------------------------------------

    async def set_network(self, policy: NetworkPolicy) -> None:
        self._ensure_usable()
        if not self._provider.capabilities().dynamic_network_policy:
            raise CapabilityNotSupported(f"{self._provider.name} has no dynamic network policy")
        if policy.mode is NetMode.OFF:
            allow_out, deny_out = None, ["0.0.0.0/0"]
        elif (
            policy.mode is NetMode.FULL
            and not policy.allow_hosts
            and not policy.allow_cidrs
            and policy.via_proxy is None
        ):
            allow_out, deny_out = ["0.0.0.0/0"], None
        else:
            _allow, network = self._provider._network_options(policy)
            allow_out = network["allow_out"] if network else None
            deny_out = None
        await asyncio.to_thread(self._provider._set_network, self._handle, allow_out, deny_out)

    async def snapshot(self, name: str) -> str:
        self._ensure_usable()
        if self._provider.capabilities().snapshot == "none":
            raise CapabilityNotSupported(f"{self._provider.name} does not support snapshots")
        snapshot_id = await asyncio.to_thread(self._provider._create_snapshot, self._handle, name)
        return str(snapshot_id)

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        try:
            await asyncio.to_thread(self._handle.kill)
        except self._provider._not_found_exceptions():
            pass
        finally:
            self._provider._sandbox_destroyed(self)

    async def describe(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.id,
            "provider": self._provider.name,
            "instance": self._provider.instance,
            "template_digest": self.spec.template.digest,
        }

    def _ensure_usable(self) -> None:
        if self._destroyed:
            raise SandboxLost(f"{self.id}: sandbox has been destroyed")


def _tar_tree(local: Path) -> bytes:
    """Pack a directory into a tar archive, preserving modes and rejecting
    symlinks that point outside the tree."""
    buffer = io.BytesIO()
    root = Path(os.path.normpath(str(local)))
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for current, dirs, files in os.walk(root):
            for name in [*dirs, *files]:
                full = Path(current) / name
                relative = full.relative_to(root)
                if full.is_symlink():
                    target = os.path.realpath(str(full))
                    if os.path.commonpath([str(root), target]) != str(root):
                        raise TransferError(
                            f"upload tree contains a symlink escaping the tree: {full} -> {target}"
                        )
                tar.add(str(full), arcname=str(relative), recursive=False)
    return buffer.getvalue()
