"""Docker sandbox provider (TECH-DESIGN section 7.4.2).

One container per trial, created from an OCI image and destroyed afterwards.
Hardening per PRD appendix D.2: read-only root filesystem, tmpfs for /tmp, the
agent home and /workspace, non-root user, ``cap_drop ALL``, no-new-privileges,
pids/cpu/memory limits, no host bind mounts and no Docker socket.

Network policy mapping (FR-SBX-03): ``off`` -> the Docker ``none`` network,
``full`` -> the default bridge. ``docs_whitelist``/``package_mirror`` need an
egress proxy: they are only offered when the provider config names an internal
Docker network (``egress_network``) and the policy carries ``via_proxy``;
anything else is rejected at create with ``PolicyNotEnforceable``. In those two modes
the proxy address is also exported to the sandbox as ``HTTP(S)_PROXY`` (see
``_proxy_env``); the internal network is what makes the proxy the only route out.
"""

from __future__ import annotations

import asyncio
import platform
import re
import time
from collections.abc import Mapping
from typing import Any

import aiodocker

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.environment import (
    ExecutionEnvironment,
    MachineFacts,
    RuntimeFacts,
    local_host_facts,
    normalise_arch,
    os_family,
)
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from .isolation import isolation_profile
from .sandbox import DockerSandbox

_LABEL_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_ZERO_DIGEST = "sha256:" + "0" * 64
#: Engine endpoints on this machine; any other scheme (tcp, ssh, http) may be remote.
_LOCAL_ENGINE_SCHEMES = ("unix://", "npipe://")
_ENGINE_CALL_TIMEOUT_S = 30.0

_ARCH_ALIASES = {
    "aarch64": Arch.ARM64,
    "arm64": Arch.ARM64,
    "x86_64": Arch.X86_64,
    "amd64": Arch.X86_64,
}


class DockerProvider(SandboxProvider):
    """Sandbox provider backed by a local Docker daemon via aiodocker."""

    name = "docker"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._runtime = str(config.get("runtime", "runc"))
        egress_network = config.get("egress_network")
        self._egress_network: str | None = None if egress_network is None else str(egress_network)
        self._max_concurrency = _config_int(config, "max_concurrency", 16)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 7200)
        self._pids_limit = _config_int(config, "pids_limit", 256)
        ceilings = {key: config.get(key) for key in ("max_cpus", "max_memory_mb", "max_disk_mb")}
        self._max_resources: Resources | None = (
            Resources(
                cpus=float(ceilings["max_cpus"] or 0) or float("inf"),
                memory_mb=int(ceilings["max_memory_mb"] or 0) or 2**31,
                disk_mb=int(ceilings["max_disk_mb"] or 0) or 2**31,
            )
            if any(value is not None for value in ceilings.values())
            else None
        )
        self._ttl_sweep_interval_s = _config_int(config, "ttl_sweep_interval_s", 30)
        self._engine_url = config.get("engine_url")
        host_machine = platform.machine().lower()
        self._host_arch = _ARCH_ALIASES.get(host_machine, Arch.ARM64)
        self._clients: dict[asyncio.AbstractEventLoop, Any] = {}
        self._sweepers: dict[asyncio.AbstractEventLoop, asyncio.Task[None]] = {}
        self._live: set[str] = set()
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    # -- client lifecycle -------------------------------------------------------

    def client(self) -> Any:
        """The aiodocker client bound to the current event loop."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            if self._engine_url is None:
                client = aiodocker.Docker()
            else:
                client = aiodocker.Docker(self._engine_url)
            self._clients[loop] = client
        return client

    async def close(self) -> None:
        """Stop the TTL sweepers and release the cached daemon clients.

        Sweepers and clients belong to the loop that created them. A loop that is already
        closed took its tasks and connections with it; a loop running in another thread is
        asked to do its own cancelling and closing.
        """
        current = asyncio.get_running_loop()
        for loop, task in list(self._sweepers.items()):
            if task.done() or loop.is_closed():
                continue
            if loop is current:
                task.cancel()
            else:
                loop.call_soon_threadsafe(task.cancel)
        self._sweepers.clear()
        for loop, client in list(self._clients.items()):
            if loop.is_closed():
                continue
            if loop is current:
                await client.close()
            else:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(client.close(), loop))
        self._clients.clear()

    def map_error(self, exc: Exception, sandbox_id: str | None = None) -> SpleefError:
        """Project aiodocker errors onto the SPI error taxonomy."""
        if isinstance(exc, SpleefError):
            return exc
        status = getattr(exc, "status", None)
        if status == 404:
            return SandboxLost(detail={"sandbox_id": sandbox_id, "cause": str(exc)})
        return SpleefError(f"docker daemon error: {exc}")

    # -- capabilities and profile ------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        net_modes = {NetMode.OFF, NetMode.FULL}
        if self._egress_network:
            net_modes |= {NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.LINUX}),
            arches=frozenset({self._host_arch}),
            isolation=Isolation.L2 if self._runtime == "runsc" else Isolation.L1,
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
            enforces_limits=frozenset({"cpu", "memory", "pids", "disk"}),
            max_concurrency=self._max_concurrency,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=self._max_resources,
            typical_start_s=1.0,
            reset_cost_s=1.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        declared = Isolation.L2 if self._runtime == "runsc" else Isolation.L1
        return isolation_profile(declared)

    async def preflight(self) -> dict[str, Any]:
        """Daemon facts used by the scheduler and the host inventory."""
        try:
            version = await self.client().version()
            info = await self.client().system.info()
        except Exception as exc:
            raise SpleefError(f"docker preflight failed: {exc}") from exc
        runtimes = sorted(info.get("Runtimes") or {})
        if self._runtime not in runtimes:
            # The declared isolation level rests on this runtime (runsc means L2).
            raise PolicyNotEnforceable(
                f"docker runtime {self._runtime!r} is not registered with the daemon "
                f"(available: {', '.join(runtimes) or 'none'})"
            )
        return {
            "provider": f"{self.name}/{self.instance}",
            "server_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "min_api_version": version.get("MinAPIVersion"),
            "daemon_os": version.get("Os"),
            "daemon_arch": version.get("Arch"),
            "kernel": version.get("KernelVersion"),
            "host_arch": self._host_arch.value,
            "runtime": self._runtime,
            "egress_network": self._egress_network,
            "max_concurrency": self._max_concurrency,
        }

    async def environment(self) -> ExecutionEnvironment:
        """Engine version, the engine's kernel and OS, and the host when the engine is local.

        The guest is the machine containers run on as the daemon reports it (the Docker
        Desktop VM on macOS, the host itself on Linux). Host facts come from this process
        only when the engine listens on a local socket: a remote engine's host is not
        this machine, so it stays unreported. Only stable daemon facts are kept (no
        container counts, clock or memory in use).
        """
        client = self.client()
        try:
            version = await asyncio.wait_for(client.version(), _ENGINE_CALL_TIMEOUT_S)
            info = await asyncio.wait_for(client.system.info(), _ENGINE_CALL_TIMEOUT_S)
        except Exception as exc:
            raise SpleefError(f"docker engine facts unavailable: {exc}") from exc
        local = str(client.docker_host).startswith(_LOCAL_ENGINE_SCHEMES)
        host = await asyncio.to_thread(local_host_facts) if local else MachineFacts()
        extra = {
            "engine_endpoint": "local" if local else "remote",
            "oci_runtime": self._runtime,
        }
        platform_name = (version.get("Platform") or {}).get("Name")
        if platform_name:
            extra["engine_platform"] = str(platform_name)
        if version.get("ApiVersion"):
            extra["api_version"] = str(version["ApiVersion"])
        for component in version.get("Components") or ():
            name, component_version = component.get("Name"), component.get("Version")
            if name and component_version and name != "Engine":
                extra[f"component.{name}"] = str(component_version)
        cpus, memory = info.get("NCPU"), info.get("MemTotal")
        return ExecutionEnvironment(
            host=host,
            runtime=RuntimeFacts(
                name="docker", version=version.get("Version") or info.get("ServerVersion")
            ),
            guest=MachineFacts(
                os=os_family(info.get("OSType") or version.get("Os")),
                os_version=info.get("OperatingSystem") or None,
                kernel=info.get("KernelVersion") or version.get("KernelVersion") or None,
                arch=normalise_arch(info.get("Architecture") or version.get("Arch")),
                cpu_count=cpus if isinstance(cpus, int) else None,
                memory_bytes=memory if isinstance(memory, int) else None,
            ),
            extra=extra,
        )

    # -- test-side controls (used by the conformance injectors) -------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    async def lose(self, sandbox_id: str) -> None:
        """Simulate the vendor reclaiming a live sandbox (docker rm -f)."""
        try:
            await self.client().containers.container(sandbox_id).delete(force=True)
        except Exception as exc:
            raise self.map_error(exc, sandbox_id) from exc

    async def expire_live_sandboxes(self) -> None:
        """Run the TTL sweep as if every live sandbox had outlived its lifetime."""
        await self._sweep_once(now=time.time() + self._max_lifetime_s + 1)

    # -- SandboxProvider -----------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        self._validate_spec(spec)
        if len(self._live) + self._pending >= self._max_concurrency:
            raise QuotaExceeded(
                f"docker provider {self.instance!r} is at max_concurrency {self._max_concurrency}",
                detail={"provider": self.instance, "max_concurrency": self._max_concurrency},
            )
        # Reserve the slot before any await: the check-and-increment is atomic
        # under asyncio, so concurrent creates cannot overshoot the limit.
        self._pending += 1
        try:
            image = await self._ensure_image(spec)
            labels = self._labels(spec)
            container: Any = None
            try:
                container = await self.client().containers.create(
                    self._container_config(spec, image, labels)
                )
                sandbox_id = container.id
                await container.start()
            except Exception as exc:
                message = f"failed to create the sandbox container: {exc}"
                if container is not None:
                    try:
                        await container.delete(force=True)
                    except Exception as cleanup_exc:
                        message += (
                            f"; the created container {container.id} could not be "
                            f"removed either: {cleanup_exc}"
                        )
                raise SandboxCreateError(message) from exc
        finally:
            self._pending -= 1
        self._live.add(sandbox_id)
        self._ensure_sweeper()
        return DockerSandbox(self, sandbox_id, spec)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """All containers (including stopped) matching every given label."""
        filters = {"label": [f"{key}={value}" for key, value in labels.items()]}
        try:
            entries = await self.client().containers.list(all=True, filters=filters)
        except Exception as exc:
            raise SpleefError(f"failed to list containers: {exc}") from exc
        return [self._summarise(entry) for entry in entries]

    @staticmethod
    def _summarise(entry: Any) -> dict[str, Any]:
        """The daemon's JSON summary behind an aiodocker DockerContainer."""
        raw = getattr(entry, "_container", entry)
        if not isinstance(raw, dict):
            raw = {}
        return {
            "id": raw.get("Id", ""),
            "labels": dict(raw.get("Labels") or {}),
            "state": raw.get("State", ""),
            "names": raw.get("Names") or [],
            "created_at": raw.get("Created") or 0,
        }

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Destroy matching orphans older than ``older_than_s`` or past their TTL."""
        now = time.time()
        reaped = 0
        for entry in await self.list(labels):
            entry_labels = entry["labels"]
            created = float(entry.get("created_at") or 0)
            expires_at = float(entry_labels.get("spleef.expires_at") or 0)
            past_age = created > 0 and (now - created) >= older_than_s
            past_ttl = expires_at > 0 and expires_at <= now
            if not (past_age or past_ttl):
                continue
            try:
                await self.client().containers.container(entry["id"]).delete(force=True)
            except Exception as exc:
                mapped = self.map_error(exc, entry["id"])
                if not isinstance(mapped, SandboxLost):
                    raise mapped from exc
            reaped += 1
        return reaped

    async def release_sandbox(self, sandbox: DockerSandbox) -> None:
        """Idempotent destroy: ``remove -f``, a missing container counts as success."""
        self._live.discard(sandbox.id)
        try:
            await self.client().containers.container(sandbox.id).delete(force=True)
        except Exception as exc:
            mapped = self.map_error(exc, sandbox.id)
            if not isinstance(mapped, SandboxLost):
                raise mapped from exc

    # -- spec validation and container config --------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> None:
        caps = self.capabilities()
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(f"docker supports linux only, got {spec.os!r}")
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(
                f"docker on {self._host_arch.value} cannot run arch {spec.arch!r}"
            )
        if spec.template.kind != "oci":
            raise CapabilityNotSupported(
                f"docker needs an oci template, got {spec.template.kind!r}"
            )
        if spec.gui:
            raise CapabilityNotSupported("docker provider does not support GUI sandboxes")
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("docker provider does not support GPUs")
        if spec.max_lifetime_s > self._max_lifetime_s:
            raise CapabilityNotSupported(
                f"lifetime {spec.max_lifetime_s} s exceeds this instance's {self._max_lifetime_s} s",
                detail={"max_lifetime_s": self._max_lifetime_s},
            )
        ceiling, wanted = self._max_resources, spec.resources
        if ceiling is not None:
            over = [
                name
                for name, asked, cap in (
                    ("cpus", wanted.cpus, ceiling.cpus),
                    ("memory_mb", wanted.memory_mb, ceiling.memory_mb),
                    ("disk_mb", wanted.disk_mb, ceiling.disk_mb),
                )
                if asked > cap
            ]
            if over:
                raise CapabilityNotSupported(
                    f"resources over this instance's ceiling: {', '.join(over)}",
                    detail={"over": over},
                )
        policy = spec.network
        if policy.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
            if not self._egress_network or not policy.via_proxy:
                raise PolicyNotEnforceable(
                    f"network mode {policy.mode.value!r} needs an egress proxy: set "
                    "config 'egress_network' and pass policy.via_proxy"
                )
        elif policy.allow_hosts or policy.allow_cidrs:
            raise PolicyNotEnforceable(
                "docker cannot enforce host/cidr allowlists natively; use "
                "docs_whitelist/package_mirror with via_proxy"
            )

    def _network_mode(self, spec: SandboxSpec) -> str:
        policy = spec.network
        if policy.mode == NetMode.OFF:
            return "none"
        if policy.mode == NetMode.FULL:
            return "bridge"
        assert self._egress_network is not None  # validated in _validate_spec
        return self._egress_network

    @staticmethod
    def _proxy_url(via_proxy: str) -> str:
        """Normalise ``host:port`` or ``http://host:port`` to one proxy URL form."""
        value = via_proxy.strip()
        return value if "://" in value else f"http://{value}"

    def _proxy_env(self, spec: SandboxSpec) -> dict[str, str]:
        """Proxy variables for the proxied modes; empty for ``off`` and ``full``."""
        policy = spec.network
        if policy.mode not in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
            return {}
        assert policy.via_proxy is not None  # validated in _validate_spec
        url = self._proxy_url(policy.via_proxy)
        loopback = "localhost,127.0.0.1,::1"
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "NO_PROXY": loopback,
            "no_proxy": loopback,
        }

    def _labels(self, spec: SandboxSpec) -> dict[str, str]:
        labels = dict(spec.labels)
        labels["spleef.provider"] = self.instance
        labels["spleef.expires_at"] = str(int(time.time()) + spec.max_lifetime_s)
        for key in labels:
            if not _LABEL_KEY_RE.match(key):
                raise SandboxCreateError(f"invalid docker label key: {key!r}")
        return labels

    def _container_config(
        self,
        spec: SandboxSpec,
        image: str,
        labels: Mapping[str, str],
    ) -> dict[str, Any]:
        user = spec.run_as_user
        memory = spec.resources.memory_mb << 20
        home_tmpfs = f"/home/{user}"
        tmpfs = {
            "/tmp": "rw,nosuid,nodev,mode=1777",
            # The rootfs is read-only, so the workspace tmpfs is the disk the spec asks for.
            "/workspace": f"rw,exec,nosuid,nodev,mode=0777,size={spec.resources.disk_mb}m",
            home_tmpfs: "rw,exec,nosuid,nodev,mode=0777",
        }
        if spec.role == "verifier":
            # Hidden tests are uploaded to and run from /verify (the evaluator's default root).
            tmpfs["/verify"] = "rw,exec,nosuid,nodev,mode=0777"
        return {
            "Image": image,
            "Cmd": ["sleep", "infinity"],
            "User": user,
            # Precedence: the policy decides the proxy, so its variables replace any
            # same-named entry in spec.env.
            "Env": [
                f"{key}={value}" for key, value in {**spec.env, **self._proxy_env(spec)}.items()
            ],
            "WorkingDir": "/workspace",
            "Labels": dict(labels),
            "Tty": False,
            "OpenStdin": False,
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Tmpfs": tmpfs,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": self._pids_limit,
                "NanoCpus": round(spec.resources.cpus * 1e9),
                "Memory": memory,
                "MemorySwap": memory,
                "NetworkMode": self._network_mode(spec),
                "Init": True,
                "AutoRemove": False,
                "Binds": [],
                # runsc (gVisor) is what the L2 declaration rests on; never fall back.
                "Runtime": self._runtime,
            },
        }

    async def _ensure_image(self, spec: SandboxSpec) -> str:
        """Resolve the template to a local image, pulling by digest when needed."""
        ref = spec.template.ref
        digest = spec.template.digest
        candidates: list[str] = []
        if digest and digest != _ZERO_DIGEST:
            candidates.append(f"{ref}@{digest}")
        candidates.append(ref)
        images = self.client().images
        for candidate in candidates:
            try:
                await images.inspect(candidate)
                return candidate
            except Exception:
                continue
        for candidate in candidates:
            try:
                await images.pull(candidate)
                return candidate
            except Exception:
                continue
        raise SandboxCreateError(
            f"image {ref!r} is not available locally and could not be pulled",
            detail={"template": spec.template},
        )

    # -- TTL sweep --------------------------------------------------------------------

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
                # The sweep is a safety net; a transient daemon error must not
                # kill the only TTL backstop this provider has.
                continue

    async def _sweep_once(self, *, now: float | None = None) -> None:
        filters = {"label": [f"spleef.provider={self.instance}"]}
        entries = await self.client().containers.list(all=True, filters=filters)
        now = time.time() if now is None else now
        for entry in entries:
            summary = self._summarise(entry)
            expires_at = float(summary["labels"].get("spleef.expires_at") or 0)
            if expires_at and expires_at <= now:
                try:
                    await self.client().containers.container(summary["id"]).delete(force=True)
                except Exception:
                    continue


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
