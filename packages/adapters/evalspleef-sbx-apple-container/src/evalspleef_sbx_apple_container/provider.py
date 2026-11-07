"""apple/container sandbox provider (TECH-DESIGN section 7.4.4).

Every container is its own lightweight Linux VM, so the isolation level is L3
and the macOS VM concurrency cap (two guests) does not apply. The provider drives
the ``container`` CLI (``run``, ``exec``, ``cp``, ``rm``, ``ls``, ``network``)
through asyncio subprocesses.

Networks are either open (the system ``default`` network) or internal
(``container network create --internal``). Host allowlists are therefore only
offered through an egress proxy: a whitelist policy needs both a configured
``egress_proxy`` and ``policy.via_proxy``; anything else is rejected at create
with ``PolicyNotEnforceable``.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import platform
import re
import secrets
import time
from collections.abc import Mapping
from typing import Any

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SpleefError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from ._cli import ContainerCLI
from .isolation import isolation_profile
from .sandbox import AppleContainerSandbox

_ZERO_DIGEST = "sha256:" + "0" * 64
_LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WHITELIST_MODES = frozenset({NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR})
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_BOOT_POLL_MIN_S = 0.05
_BOOT_POLL_MAX_S = 1.0


class AppleContainerProvider(SandboxProvider):
    """Sandbox provider backed by the macOS 26 ``container`` CLI."""

    name = "apple-container"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._executable = str(config.get("container_bin", "container"))
        cli_env = config.get("cli_env") or {}
        if not isinstance(cli_env, Mapping):
            raise SandboxCreateError("config key 'cli_env' must be a mapping")
        self.cli = ContainerCLI(self._executable, {str(k): str(v) for k, v in cli_env.items()})
        egress_proxy = config.get("egress_proxy")
        self._egress_proxy: str | None = None if egress_proxy is None else str(egress_proxy)
        self._internal_network = str(config.get("internal_network", "spleef-internal"))
        self._open_network = str(config.get("open_network", "default"))
        self._max_concurrency = _config_int(config, "max_concurrency", 8)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 7200)
        self._boot_timeout_s = float(config.get("boot_timeout_s", 90.0))
        self.transfer_timeout_s = float(config.get("transfer_timeout_s", 300.0))
        self._ttl_sweep_interval_s = _config_int(config, "ttl_sweep_interval_s", 30)
        self._host_arch = _ARCH_ALIASES.get(platform.machine().lower(), Arch.ARM64)
        self._sweepers: dict[asyncio.AbstractEventLoop, asyncio.Task[None]] = {}
        self._live: set[str] = set()
        self._networks_ready: set[str] = set()
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    # -- capabilities and profile -----------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        net_modes = {NetMode.OFF, NetMode.FULL}
        if self._egress_proxy:
            net_modes |= set(_WHITELIST_MODES)
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.LINUX}),
            arches=frozenset({Arch.ARM64}),
            isolation=Isolation.L3,
            net_modes=frozenset(net_modes),
            allowlist_kinds=frozenset(),
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
            max_concurrency=self._max_concurrency,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=2.0,
            reset_cost_s=1.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile(Isolation.L3)

    async def preflight(self) -> dict[str, Any]:
        """CLI version and host facts for the host inventory."""
        try:
            result = await self.cli.run(["system", "version", "--format", "json"], timeout_s=30.0)
        except TimeoutError as exc:
            raise SpleefError(f"apple-container preflight timed out: {exc}") from exc
        if result.exit_code != 0:
            raise SpleefError(
                "apple-container preflight failed; is the container system running?",
                detail={"exit_code": result.exit_code, "stderr": result.stderr_text[:400]},
            )
        version: Any = result.stdout_text.strip()
        try:
            parsed = json.loads(version)
            if isinstance(parsed, dict):
                version = parsed.get("version") or parsed.get("Version") or version
        except ValueError:
            pass
        return {
            "provider": f"{self.name}/{self.instance}",
            "version": version,
            "executable": self._executable,
            "host_os": platform.system().lower(),
            "host_arch": self._host_arch.value,
            "max_concurrency": self._max_concurrency,
            "max_lifetime_s": self._max_lifetime_s,
            "internal_network": self._internal_network,
            "open_network": self._open_network,
            "egress_proxy": self._egress_proxy,
        }

    # -- test-side controls (used by the conformance injectors) -------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        self._fail_next.extend([error] * n)

    async def lose(self, sandbox_id: str) -> None:
        """Simulate the vendor reclaiming a live sandbox."""
        self._live.discard(sandbox_id)
        result = await self.cli.run(["rm", "--force", sandbox_id])
        if result.exit_code != 0 and await self.container_exists(sandbox_id):
            raise SpleefError(f"failed to remove sandbox {sandbox_id!r}: {result.stderr_text}")

    async def expire_live_sandboxes(self) -> None:
        """Remove every live sandbox, mirroring a TTL expiry sweep."""
        for sandbox_id in list(self._live):
            await self.lose(sandbox_id)

    async def close(self) -> None:
        for task in self._sweepers.values():
            task.cancel()
        self._sweepers.clear()

    # -- SandboxProvider ---------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        network_env = self._validate_spec(spec)
        if len(self._live) + self._pending >= self._max_concurrency:
            raise QuotaExceeded(
                f"apple-container provider {self.instance!r} is at max_concurrency "
                f"{self._max_concurrency}",
                detail={"provider": self.instance, "max_concurrency": self._max_concurrency},
            )
        self._pending += 1
        sandbox_id = f"c-{secrets.token_hex(8)}"
        try:
            network = self._network_for(spec)
            await self._ensure_network(network)
            labels = self._labels(spec)
            args = self._run_args(spec, sandbox_id, network, labels)
            result = await self.cli.run(args, timeout_s=self._boot_timeout_s)
        except TimeoutError as exc:
            self._pending -= 1
            raise SandboxCreateError(f"apple-container create timed out: {exc}") from exc
        except BaseException:
            self._pending -= 1
            raise
        if result.exit_code != 0:
            self._pending -= 1
            raise SandboxCreateError(
                "failed to start the sandbox container",
                detail={"exit_code": result.exit_code, "stderr": result.stderr_text[:800]},
            )
        try:
            await self._wait_running(sandbox_id)
        except BaseException:
            self._pending -= 1
            await self._remove_best_effort(sandbox_id)
            raise
        self._pending -= 1
        self._live.add(sandbox_id)
        self._ensure_sweeper()
        return AppleContainerSandbox(self, sandbox_id, spec, network_env)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """Containers (including stopped ones) matching every given label."""
        try:
            result = await self.cli.run(["list", "--all", "--format", "json"])
        except TimeoutError as exc:
            raise SpleefError(f"failed to list containers: {exc}") from exc
        if result.exit_code != 0:
            raise SpleefError(
                f"failed to list containers: {result.stderr_text.strip()[:400]}",
                detail={"exit_code": result.exit_code},
            )
        entries = _parse_json_list(result.stdout_text, "container list")
        matched: list[dict[str, Any]] = []
        for entry in entries:
            summary = _summarise(entry)
            if all(summary["labels"].get(key) == value for key, value in labels.items()):
                matched.append(summary)
        return matched

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Remove matching containers older than ``older_than_s`` or past their TTL."""
        now = time.time()
        reaped = 0
        for entry in await self.list(labels):
            entry_labels = entry["labels"]
            created = float(entry_labels.get("spleef.created_at") or 0)
            expires_at = float(entry_labels.get("spleef.expires_at") or 0)
            past_age = created > 0 and (now - created) >= older_than_s
            past_ttl = expires_at > 0 and expires_at <= now
            if not (past_age or past_ttl):
                continue
            await self.release_sandbox_id(str(entry["id"]))
            reaped += 1
        return reaped

    async def release_sandbox(self, sandbox: AppleContainerSandbox) -> None:
        self._live.discard(sandbox.id)
        await self.release_sandbox_id(sandbox.id)

    async def release_sandbox_id(self, sandbox_id: str) -> None:
        """Idempotent removal: a missing container counts as already removed."""
        self._live.discard(sandbox_id)
        try:
            result = await self.cli.run(["rm", "--force", sandbox_id])
        except TimeoutError as exc:
            raise SpleefError(f"failed to remove sandbox {sandbox_id!r}: {exc}") from exc
        if result.exit_code != 0 and await self.container_exists(sandbox_id):
            raise SpleefError(
                f"failed to remove sandbox {sandbox_id!r}: {result.stderr_text.strip()[:400]}"
            )

    async def inspect(self, sandbox_id: str) -> dict[str, Any] | None:
        """The CLI's inspect object for one container, or ``None`` when it is gone."""
        try:
            result = await self.cli.run(["inspect", sandbox_id], timeout_s=30.0)
        except TimeoutError:
            return None
        if result.exit_code != 0:
            return None
        try:
            parsed = json.loads(result.stdout_text)
        except ValueError:
            return None
        if isinstance(parsed, list) and parsed:
            first = parsed[0]
            return first if isinstance(first, dict) else None
        return parsed if isinstance(parsed, dict) else None

    async def container_exists(self, sandbox_id: str) -> bool:
        return await self.inspect(sandbox_id) is not None

    # -- spec validation ---------------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> dict[str, str]:
        if spec.os is not OSFamily.LINUX:
            raise CapabilityNotSupported(f"apple-container runs linux guests only, got {spec.os!r}")
        if spec.arch is not Arch.ARM64:
            raise CapabilityNotSupported(
                f"apple-container on Apple silicon cannot run arch {spec.arch!r}"
            )
        if spec.template.kind != "oci":
            raise CapabilityNotSupported(
                f"apple-container needs an oci template, got {spec.template.kind!r}"
            )
        if spec.gui:
            raise CapabilityNotSupported("apple-container provider does not support GUI sandboxes")
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("apple-container provider does not support GPUs")
        if spec.resources.cpus <= 0 or spec.resources.memory_mb <= 0:
            raise CapabilityNotSupported("cpu and memory requests must be positive")
        policy = spec.network
        if policy.allow_cidrs:
            raise PolicyNotEnforceable(
                "apple-container has no native cidr filter; use a whitelist mode through the "
                "egress proxy"
            )
        if policy.mode in _WHITELIST_MODES:
            if not self._egress_proxy or not policy.via_proxy:
                raise PolicyNotEnforceable(
                    f"network mode {policy.mode.value!r} needs an egress proxy: set config "
                    "'egress_proxy' and pass policy.via_proxy"
                )
            return self._proxy_env(policy.via_proxy)
        if policy.allow_hosts:
            raise PolicyNotEnforceable(
                f"network mode {policy.mode.value!r} cannot enforce host allowlists; use "
                "docs_whitelist or package_mirror through the egress proxy"
            )
        return {}

    def _network_for(self, spec: SandboxSpec) -> str:
        if spec.network.mode is NetMode.FULL:
            return self._open_network
        return self._internal_network

    @staticmethod
    def _proxy_env(proxy: str) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in _PROXY_VARIABLES:
            env[name] = f"http://{proxy}"
            env[name.lower()] = f"http://{proxy}"
        return env

    def _labels(self, spec: SandboxSpec) -> dict[str, str]:
        now = int(time.time())
        lifetime = min(spec.max_lifetime_s, self._max_lifetime_s)
        labels = dict(spec.labels)
        labels["spleef.provider"] = self.instance
        labels["spleef.created_at"] = str(now)
        labels["spleef.expires_at"] = str(now + lifetime)
        for key, value in labels.items():
            if not _LABEL_KEY_RE.match(key) or any(char in value for char in ("\n", "\r")):
                raise SandboxCreateError(f"invalid container label {key!r}")
        return labels

    def _run_args(
        self,
        spec: SandboxSpec,
        sandbox_id: str,
        network: str,
        labels: Mapping[str, str],
    ) -> builtins.list[str]:
        image = spec.template.ref
        if spec.template.digest and spec.template.digest != _ZERO_DIGEST:
            image = f"{image}@{spec.template.digest}"
        user = spec.run_as_user
        args = [
            "run",
            "--detach",
            "--name",
            sandbox_id,
            "--cpus",
            f"{spec.resources.cpus:g}",
            "--memory",
            f"{spec.resources.memory_mb}M",
            "--user",
            user,
            "--workdir",
            "/workspace",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/workspace",
            "--tmpfs",
            f"/home/{user}",
            "--cap-drop",
            "ALL",
            "--init",
            "--network",
            network,
        ]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in spec.env.items():
            args += ["--env", f"{key}={value}"]
        args += [image, "sleep", "infinity"]
        return args

    async def _ensure_network(self, name: str) -> None:
        if name == self._open_network or name in self._networks_ready:
            return
        result = await self.cli.run(["network", "list", "--format", "json"], timeout_s=30.0)
        names = _network_names(result.stdout_text) if result.exit_code == 0 else set()
        if name not in names:
            created = await self.cli.run(["network", "create", "--internal", name], timeout_s=60.0)
            if created.exit_code != 0:
                again = await self.cli.run(["network", "list", "--format", "json"], timeout_s=30.0)
                if again.exit_code != 0 or name not in _network_names(again.stdout_text):
                    raise SandboxCreateError(
                        f"failed to create internal network {name!r}",
                        detail={"stderr": created.stderr_text[:400]},
                    )
        self._networks_ready.add(name)

    async def _wait_running(self, sandbox_id: str) -> None:
        deadline = time.monotonic() + self._boot_timeout_s
        delay = _BOOT_POLL_MIN_S
        last_error = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxCreateError(
                    f"sandbox {sandbox_id!r} did not become ready: {last_error}",
                    detail={"sandbox_id": sandbox_id},
                )
            try:
                probe = await self.cli.run(
                    ["exec", sandbox_id, "true"],
                    timeout_s=min(10.0, max(1.0, remaining)),
                )
            except TimeoutError:
                probe = None
                last_error = "exec probe timed out"
            if probe is not None:
                if probe.exit_code == 0:
                    return
                last_error = probe.stderr_text.strip()[:400]
            if not await self.container_exists(sandbox_id):
                raise SandboxCreateError(
                    f"sandbox {sandbox_id!r} exited during start-up: {last_error}",
                    detail={"sandbox_id": sandbox_id},
                )
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, _BOOT_POLL_MAX_S)

    async def _remove_best_effort(self, sandbox_id: str) -> None:
        try:
            await self.cli.run(["rm", "--force", sandbox_id], timeout_s=30.0)
        except TimeoutError:
            return

    # -- TTL sweep ---------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        loop = asyncio.get_running_loop()
        task = self._sweepers.get(loop)
        if task is not None and not task.done():
            return
        self._sweepers[loop] = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ttl_sweep_interval_s)
            try:
                await self._sweep_once()
            except Exception:
                # A transient CLI error must not kill the only TTL backstop.
                continue

    async def _sweep_once(self) -> None:
        now = time.time()
        for entry in await self.list({"spleef.provider": self.instance}):
            expires_at = float(entry["labels"].get("spleef.expires_at") or 0)
            if expires_at and expires_at <= now:
                await self.release_sandbox_id(str(entry["id"]))


_ARCH_ALIASES = {
    "aarch64": Arch.ARM64,
    "arm64": Arch.ARM64,
    "x86_64": Arch.X86_64,
    "amd64": Arch.X86_64,
}


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_json_list(payload: str, source: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(payload or "[]")
    except ValueError as exc:
        raise SpleefError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise SpleefError(f"{source} returned {type(parsed).__name__}, expected a list")
    return [entry for entry in parsed if isinstance(entry, dict)]


def _network_names(payload: str) -> set[str]:
    try:
        parsed = json.loads(payload or "[]")
    except ValueError:
        return set()
    names: set[str] = set()
    if not isinstance(parsed, list):
        return names
    for entry in parsed:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict):
            for key in ("name", "id", "configuration"):
                value = entry.get(key)
                if isinstance(value, str):
                    names.add(value)
                elif isinstance(value, dict) and isinstance(value.get("name"), str):
                    names.add(value["name"])
    return names


def _entry_configuration(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    configuration = entry.get("configuration")
    return configuration if isinstance(configuration, Mapping) else {}


def _labels_of(entry: Mapping[str, Any]) -> dict[str, str]:
    raw = _entry_configuration(entry).get("labels", entry.get("labels"))
    labels: dict[str, str] = {}
    if isinstance(raw, Mapping):
        labels = {str(key): str(value) for key, value in raw.items()}
    elif isinstance(raw, list):
        for item in raw:
            key, separator, value = str(item).partition("=")
            if separator:
                labels[key] = value
    return labels


def _summarise(entry: Mapping[str, Any]) -> dict[str, Any]:
    configuration = _entry_configuration(entry)
    status = entry.get("status")
    state = ""
    if isinstance(status, Mapping):
        state = str(status.get("state") or "")
    labels = _labels_of(entry)
    created = labels.get("spleef.created_at") or configuration.get("creationDate") or 0
    return {
        "id": str(configuration.get("id") or entry.get("id") or ""),
        "labels": labels,
        "state": state or str(entry.get("state") or ""),
        "created_at": created,
    }
