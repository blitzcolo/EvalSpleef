"""Harbor BaseEnvironment bridge for EvalSpleef sandboxes (TECH-DESIGN section 9.1).

Exposes any registered EvalSpleef SandboxProvider as a Harbor environment so Harbor tasks
can run on any of our sandboxes:
    harbor run -e evalspleef_harbor_bridge.env:SpleefEnvironment --ek provider=docker

Harbor is NOT installed in this environment. This module subclasses Harbor's
BaseEnvironment when importable, otherwise a local shim class that mirrors its
documented signatures.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported, SpleefError
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.spi.secrets import SecretResolver

# ---------------------------------------------------------------------------
# Harbor BaseEnvironment & ExecResult shim or real import
# ---------------------------------------------------------------------------


class BaseEnvironment:
    """BaseEnvironment implementation or shim when Harbor is not installed."""

    def __init__(self, task_env_config: Any = None, **kwargs: Any) -> None:
        self.task_env_config = task_env_config
        self.kwargs = kwargs

    def type(self) -> str:
        raise NotImplementedError

    async def start(self, force_build: bool = False) -> None:
        raise NotImplementedError

    async def stop(self, delete: bool = True) -> None:
        raise NotImplementedError

    async def exec(
        self,
        command: str | list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        user: str | None = None,
    ) -> Any:
        raise NotImplementedError

    async def upload_file(self, source: Path | str, target: Path | str) -> None:
        raise NotImplementedError

    async def upload_dir(self, source: Path | str, target: Path | str) -> None:
        raise NotImplementedError

    async def download_file(self, source: Path | str, target: Path | str) -> None:
        raise NotImplementedError

    async def download_dir(self, source: Path | str, target: Path | str) -> None:
        raise NotImplementedError

    @property
    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError


# When Harbor is installed at runtime, ensure SpleefEnvironment inherits from it
if not TYPE_CHECKING:
    try:
        import harbor.environments.base as _harbor_env

        if hasattr(_harbor_env, "BaseEnvironment"):
            BaseEnvironment = _harbor_env.BaseEnvironment  # type: ignore[misc]
    except ImportError:
        pass


@dataclass(frozen=True)
class HarborExecResult:
    """Shim of Harbor's ExecResult(stdout, stderr, return_code)."""

    stdout: str
    stderr: str
    return_code: int


if not TYPE_CHECKING:
    try:
        import harbor.environments.base as _harbor_env

        if hasattr(_harbor_env, "ExecResult"):
            HarborExecResult = _harbor_env.ExecResult  # type: ignore[misc]
    except ImportError:
        pass


class _SimpleSecretResolver:
    """Fallback secret resolver using environment variables."""

    def resolve(self, secret_ref: str) -> str:
        if secret_ref.startswith("env://"):
            var_name = secret_ref.removeprefix("env://")
            val = os.environ.get(var_name)
            if val is not None:
                return val
        return ""


def find_sandbox_provider(
    provider_name: str,
    *,
    instance_name: str | None = None,
    config: Mapping[str, Any] | None = None,
    secrets: SecretResolver | None = None,
) -> SandboxProvider:
    """Find and instantiate a SandboxProvider from the entry point group."""
    group = "evalspleef.sandbox_providers"
    entry_points = importlib.metadata.entry_points(group=group)
    for ep in entry_points:
        if ep.name == provider_name:
            provider_cls = ep.load()
            instance = instance_name or provider_name
            cfg = config if config is not None else {}
            sec = secrets if secrets is not None else _SimpleSecretResolver()
            return provider_cls(instance=instance, config=cfg, secrets=sec)  # type: ignore[no-any-return]
    raise LookupError(f"No sandbox provider entry point found for {provider_name!r} in {group}")


def map_sandbox_capabilities(caps: SandboxCapabilities) -> dict[str, bool]:
    """Map EvalSpleef's SandboxCapabilities to Harbor's capability dictionary.

    TECH-DESIGN section 9.1:
      - disable_internet <= NetMode.OFF in net_modes
      - network_allowlist_hostnames <= "hostname" in allowlist_kinds
      - dynamic_network_policy <= caps.dynamic_network_policy
      - gpus <= caps.gpu
      - windows <= OSFamily.WINDOWS in os_families
      - stream <= caps.streaming
    """
    allowlist_hostnames = any(
        k == "hostname" or getattr(k, "value", None) == "hostname" for k in caps.allowlist_kinds
    )
    has_windows = any(
        os_f == OSFamily.WINDOWS or getattr(os_f, "value", None) == "windows"
        for os_f in caps.os_families
    )
    has_off = any(m == NetMode.OFF or getattr(m, "value", None) == "off" for m in caps.net_modes)

    return {
        "disable_internet": has_off,
        "network_allowlist_hostnames": allowlist_hostnames,
        "dynamic_network_policy": bool(caps.dynamic_network_policy),
        "gpus": bool(caps.gpu),
        "windows": has_windows,
        "stream": bool(caps.streaming),
    }


_PINNED = re.compile(r"sha256:[0-9a-f]{64}")


class TemplateNotPinned(SpleefError):
    """No digest is known for the task image; a template is never run by tag alone."""

    error_class = "environment"
    disposition = "manual"


def resolve_digest(image: str, explicit: str | None, registry: Mapping[str, Any] | None) -> str:
    """The image digest: explicit, pinned in the reference, or built and recorded in the registry."""
    if explicit:
        if not _PINNED.fullmatch(explicit):
            raise TemplateNotPinned(f"digest {explicit!r} is not sha256:<64 hex>")
        return explicit
    _, at, pinned = image.partition("@")
    if at and _PINNED.fullmatch(pinned):
        return pinned
    for entry in (registry or {}).values():
        if isinstance(entry, Mapping) and entry.get("tag") == image:
            recorded = str(entry.get("image_id") or "")
            if _PINNED.fullmatch(recorded):
                return recorded
    raise TemplateNotPinned(
        f"no digest for {image!r}: pin it (@sha256:...), pass digest, or build it into the "
        "template registry",
        detail={"image": image},
    )


class SpleefEnvironment(BaseEnvironment):
    """Bridges Harbor's BaseEnvironment calls to an EvalSpleef Sandbox instance."""

    def __init__(
        self,
        task_env_config: Any = None,
        *,
        provider: str | SandboxProvider = "docker",
        config_path: str | Path | None = None,
        provider_config: Mapping[str, Any] | None = None,
        secrets: SecretResolver | None = None,
        templates_registry: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(task_env_config=task_env_config, **kwargs)
        self._provider_arg = provider
        self._config_path = config_path
        self._provider_config = dict(provider_config or {})
        self._secrets = secrets
        self._registry: dict[str, Any] = (
            json.loads(Path(templates_registry).read_text(encoding="utf-8"))
            if templates_registry
            else {}
        )
        self._provider: SandboxProvider | None = None
        self._sandbox: Sandbox | None = None

        if isinstance(provider, SandboxProvider):
            self._provider = provider

    def _ensure_provider(self) -> SandboxProvider:
        if self._provider is not None:
            return self._provider

        if isinstance(self._provider_arg, str):
            provider_name = self._provider_arg
            # If config_path is given, try reading JSON/YAML config if any
            cfg = dict(self._provider_config)
            if self._config_path:
                path = Path(self._config_path)
                if path.is_file():
                    with contextlib.suppress(Exception):
                        cfg.update(json.loads(path.read_text(encoding="utf-8")))
            self._provider = find_sandbox_provider(
                provider_name,
                config=cfg,
                secrets=self._secrets,
            )
            return self._provider

        raise TypeError(f"Invalid provider specification: {self._provider_arg!r}")

    @property
    def sandbox(self) -> Sandbox | None:
        return self._sandbox

    def type(self) -> str:
        return "evalspleef"

    @property
    def capabilities(self) -> dict[str, Any]:
        provider = self._ensure_provider()
        caps = provider.capabilities()
        return map_sandbox_capabilities(caps)

    async def start(self, force_build: bool = False) -> None:
        """Create and start the sandbox matching task_env_config."""
        del force_build
        provider = self._ensure_provider()

        spec = self._build_spec(self.task_env_config)
        self._sandbox = await provider.create(spec)

    def _build_spec(self, env_cfg: Any) -> SandboxSpec:
        """Build a SandboxSpec from Harbor task environment config."""
        image = "ubuntu:24.04"
        cpus = 2.0
        memory_mb = 4096
        storage_mb = 10240
        gpus = 0
        os_family = OSFamily.LINUX
        arch = Arch.X86_64
        net_mode = NetMode.FULL
        env_vars: dict[str, str] = {}
        digest: str | None = None

        if env_cfg is not None:
            if isinstance(env_cfg, dict):
                image = str(env_cfg.get("image", image))
                digest = env_cfg.get("digest")
                cpus = float(env_cfg.get("cpus", cpus))
                memory_mb = int(env_cfg.get("memory_mb", memory_mb))
                storage_mb = int(env_cfg.get("storage_mb", storage_mb))
                gpus = int(env_cfg.get("gpus", gpus))
                if env_cfg.get("os") in ("windows", OSFamily.WINDOWS):
                    os_family = OSFamily.WINDOWS
                if env_cfg.get("arch") in ("arm64", Arch.ARM64):
                    arch = Arch.ARM64
                if env_cfg.get("net_mode"):
                    net_mode = NetMode(env_cfg["net_mode"])
                if isinstance(env_cfg.get("env"), dict):
                    env_vars = {str(k): str(v) for k, v in env_cfg["env"].items()}
            else:
                image = getattr(env_cfg, "image", image)
                digest = getattr(env_cfg, "digest", None)
                cpus = float(getattr(env_cfg, "cpus", cpus))
                memory_mb = int(getattr(env_cfg, "memory_mb", memory_mb))
                storage_mb = int(getattr(env_cfg, "storage_mb", storage_mb))
                gpus = int(getattr(env_cfg, "gpus", gpus))
                if getattr(env_cfg, "os", None) in ("windows", OSFamily.WINDOWS):
                    os_family = OSFamily.WINDOWS
                if getattr(env_cfg, "arch", None) in ("arm64", Arch.ARM64):
                    arch = Arch.ARM64
                if getattr(env_cfg, "net_mode", None):
                    net_mode = NetMode(env_cfg.net_mode)
                if getattr(env_cfg, "env", None) and isinstance(env_cfg.env, dict):
                    env_vars = {str(k): str(v) for k, v in env_cfg.env.items()}

        resources = Resources(
            cpus=cpus,
            memory_mb=memory_mb,
            disk_mb=storage_mb,
            gpus=gpus,
        )
        template = TemplateRef(
            kind="oci",
            ref=image,
            digest=resolve_digest(image, digest, self._registry),
        )
        network = NetworkPolicy(mode=net_mode)

        import uuid

        trial_id = f"harbor-{uuid.uuid4().hex[:8]}"
        lease_id = f"lease-{uuid.uuid4().hex[:8]}"

        return SandboxSpec(
            trial_id=trial_id,
            lease_id=lease_id,
            role="agent",
            os=os_family,
            arch=arch,
            template=template,
            resources=resources,
            network=network,
            env=env_vars,
            labels={"harbor": "true"},
        )

    async def stop(self, delete: bool = True) -> None:
        """Destroy the sandbox. Always destroys even if delete=False."""
        del delete
        if self._sandbox is not None:
            await self._sandbox.destroy()
            self._sandbox = None

    async def exec(
        self,
        command: str | list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        user: str | None = None,
    ) -> HarborExecResult:
        """Run command in sandbox and return Harbor's ExecResult."""
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")

        result = await self._sandbox.exec(
            command,
            cwd=cwd,
            env=env,
            timeout_s=timeout_sec,
            user=user,
        )

        stdout_str = result.stdout.decode("utf-8", errors="replace")
        stderr_str = result.stderr.decode("utf-8", errors="replace")
        return HarborExecResult(
            stdout=stdout_str,
            stderr=stderr_str,
            return_code=result.exit_code,
        )

    async def upload_file(self, source: Path | str, target: Path | str) -> None:
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")
        await self._sandbox.upload(Path(source), str(target))

    async def upload_dir(self, source: Path | str, target: Path | str) -> None:
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")
        await self._sandbox.upload(Path(source), str(target))

    async def download_file(self, source: Path | str, target: Path | str) -> None:
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")
        await self._sandbox.download(str(source), Path(target))

    async def download_dir(self, source: Path | str, target: Path | str) -> None:
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")
        await self._sandbox.download(str(source), Path(target))

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        """Set dynamic network policy when supported, otherwise raise CapabilityNotSupported."""
        provider = self._ensure_provider()
        caps = provider.capabilities()
        if not caps.dynamic_network_policy:
            raise CapabilityNotSupported("dynamic_network_policy")
        if self._sandbox is None:
            raise RuntimeError("Environment is not started: sandbox is None")
        await self._sandbox.set_network(policy)
