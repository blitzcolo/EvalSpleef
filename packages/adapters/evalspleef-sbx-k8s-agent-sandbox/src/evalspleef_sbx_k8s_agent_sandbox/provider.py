"""Kubernetes agent-sandbox provider (TECH-DESIGN section 7.4.8).

Each trial gets one ``Sandbox`` custom resource from the kubernetes-sigs
agent-sandbox project, created either directly or by claiming a pre-warmed
instance from a ``SandboxWarmPool``. Isolation is selected by ``RuntimeClass``:
gVisor (``gvisor``/``runsc``) is L2, Kata (``kata-*``) is L3. Egress is governed
by a generated Cilium FQDN policy. ``list``/``reap`` work on object labels and a
per-sandbox lifetime; warm-pool instances are only claimed, never returned.

The CRD contract was checked against the upstream manifests on 2026-11-07:
``Sandbox`` is ``agents.x-k8s.io/v1beta1``; ``SandboxTemplate``,
``SandboxWarmPool`` and ``SandboxClaim`` are
``extensions.agents.x-k8s.io/v1beta1``. See the package README for URLs.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

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
    EnforcedLimit,
    ExecResult,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from .isolation import isolation_profile, linux_probe_script
from .kube import (
    API_VERSION,
    EXTENSIONS_GROUP,
    SANDBOX_CLAIM_PLURAL,
    SANDBOX_GROUP,
    SANDBOX_PLURAL,
    KubeApi,
    RealKubeApi,
)
from .network import CILIUM_GROUP, CILIUM_PLURAL, CILIUM_VERSION, build_egress_policy
from .sandbox import CONTAINER_NAME, K8sAgentSandbox

_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_DNS_PREFIX_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_GVISOR_CLASSES = {"gvisor", "runsc"}
_KATA_PREFIX = "kata"
_SUPPORTED_LIMITS = {"cpu", "memory", "disk"}


class K8sAgentSandboxProvider(SandboxProvider):
    """Sandbox provider backed by the agent-sandbox CRDs on a Kubernetes cluster."""

    name = "k8s-agent-sandbox"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        api: KubeApi | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(instance, config, secrets)
        self.namespace = str(config.get("namespace", "default"))
        self.runtime_class = str(config.get("runtime_class", "gvisor"))
        self._declared_isolation = _isolation_for(self.runtime_class)
        warm_pool = config.get("warm_pool")
        self._warm_pool: str | None = None if warm_pool in (None, "") else str(warm_pool)
        self._max_concurrency = _config_int(config, "max_concurrency", 16)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 7200)
        self._ready_timeout_s = float(config.get("ready_timeout_s", 120.0))
        self._ready_poll_s = float(config.get("ready_poll_s", 0.5))
        limits = config.get("enforces_limits", ["cpu", "memory"])
        self._enforces_limits = frozenset(str(item) for item in limits)
        unknown = self._enforces_limits - _SUPPORTED_LIMITS
        if unknown:
            raise ValueError(f"unknown enforced limits: {sorted(unknown)}")
        self._clock = clock or time.time
        self._api: KubeApi = api or RealKubeApi(
            context=_optional_str(config.get("context")),
            kubeconfig=_optional_str(config.get("kubeconfig")),
        )
        self._live: set[str] = set()
        self._claims: set[str] = set()
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    @property
    def api(self) -> KubeApi:
        """The Kubernetes transport this provider uses (tests and conformance inspect it)."""
        return self._api

    # -- capabilities and profile ------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.LINUX}),
            arches=frozenset({Arch.X86_64, Arch.ARM64}),
            isolation=self._declared_isolation,
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
            enforces_limits=cast(frozenset[EnforcedLimit], self._enforces_limits),
            max_concurrency=self._max_concurrency,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=2.0,
            reset_cost_s=2.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile(self._declared_isolation)

    async def preflight(self) -> dict[str, Any]:
        """Check that the agent-sandbox CRDs answer and report the instance facts."""
        try:
            sandboxes = await self._api.list_objects(
                group=SANDBOX_GROUP,
                version=API_VERSION,
                plural=SANDBOX_PLURAL,
                namespace=self.namespace,
                label_selector=f"spleef.provider={_label_value(self.instance)}",
            )
        except SpleefError as exc:
            raise SpleefError(f"k8s agent-sandbox preflight failed: {exc}") from exc
        return {
            "provider": f"{self.name}/{self.instance}",
            "namespace": self.namespace,
            "runtime_class": self.runtime_class,
            "declared_isolation": f"L{int(self._declared_isolation)}",
            "warm_pool": self._warm_pool,
            "crds_ready": True,
            "live_sandboxes": len(sandboxes),
            "max_concurrency": self._max_concurrency,
        }

    # -- test-side controls -------------------------------------------------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    async def lose(self, sandbox_id: str) -> None:
        """Simulate the vendor reclaiming a live sandbox."""
        await self._api.delete_object(
            group=SANDBOX_GROUP,
            version=API_VERSION,
            plural=SANDBOX_PLURAL,
            namespace=self.namespace,
            name=sandbox_id,
        )

    async def expire_live_sandboxes(self) -> None:
        """Delete every live sandbox, mirroring lifetime expiry enforced by the cluster."""
        for name in list(self._live):
            await self._delete_objects(name)
            self._live.discard(name)

    async def close(self) -> None:
        await self._api.close()

    async def run_probe(self, sandbox: Sandbox) -> ExecResult:
        """Run the shipped Linux isolation probe through ``sandbox.exec``."""
        return await sandbox.exec(linux_probe_script(), timeout_s=120.0)

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
                f"k8s agent-sandbox provider {self.instance!r} is at max_concurrency "
                f"{self._max_concurrency}",
                detail={"provider": self.instance, "max_concurrency": self._max_concurrency},
            )
        self._pending += 1
        name = f"spleef-{uuid.uuid4().hex[:12]}"
        labels = self._labels(spec)
        policy = build_egress_policy(
            sandbox_name=name,
            namespace=self.namespace,
            pod_labels=labels,
            policy=spec.network,
        )
        claimed = self._warm_pool is not None
        try:
            if policy is not None:
                await self._api.create_object(
                    group=CILIUM_GROUP,
                    version=CILIUM_VERSION,
                    plural=CILIUM_PLURAL,
                    namespace=self.namespace,
                    body=policy,
                )
            if claimed:
                await self._create_claim(name, spec, labels)
            else:
                await self._api.create_object(
                    group=SANDBOX_GROUP,
                    version=API_VERSION,
                    plural=SANDBOX_PLURAL,
                    namespace=self.namespace,
                    body=self._sandbox_body(name, spec, labels),
                )
            await self._wait_ready(name)
        except Exception as exc:
            await self._delete_objects(name)
            if isinstance(exc, SpleefError):
                raise
            raise SandboxCreateError(
                f"failed to create kubernetes agent sandbox {name!r}: {exc}"
            ) from exc
        finally:
            self._pending -= 1
        self._live.add(name)
        if claimed:
            self._claims.add(name)
        return K8sAgentSandbox(self, name, spec, claimed=claimed)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """Sandbox objects matching every given label."""
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        objects = await self._api.list_objects(
            group=SANDBOX_GROUP,
            version=API_VERSION,
            plural=SANDBOX_PLURAL,
            namespace=self.namespace,
            label_selector=selector,
        )
        return [self._summarise(body) for body in objects]

    @staticmethod
    def _summarise(body: Mapping[str, Any]) -> dict[str, Any]:
        metadata = body.get("metadata", {})
        annotations = metadata.get("annotations", {})
        labels = dict(metadata.get("labels") or {})
        status = body.get("status", {})
        ready: dict[str, Any] = next(
            (
                condition
                for condition in status.get("conditions", [])
                if condition.get("type") == "Ready"
            ),
            {},
        )
        created = _float_or_zero(
            labels.get("spleef.created_at") or annotations.get("spleef.created_at")
        )
        expires = _float_or_zero(labels.get("spleef.expires_at"))
        return {
            "id": str(metadata.get("name", "")),
            "labels": labels,
            "state": ready.get("status", "Unknown"),
            "reason": ready.get("reason", ""),
            "created_at": created,
            "expires_at": expires,
        }

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Delete matching orphans older than ``older_than_s`` or past their lifetime."""
        now = self._clock()
        reaped = 0
        for entry in await self.list(labels):
            created = float(entry.get("created_at") or 0)
            expires = float(entry.get("expires_at") or 0)
            past_age = created > 0 and (now - created) >= older_than_s
            past_lifetime = expires > 0 and expires <= now
            if not (past_age or past_lifetime):
                continue
            name = str(entry["id"])
            await self._delete_objects(name)
            self._live.discard(name)
            self._claims.discard(name)
            reaped += 1
        return reaped

    async def release_sandbox(self, sandbox: K8sAgentSandbox) -> None:
        """Idempotent destroy: delete the policy, the claim and the Sandbox object."""
        self._live.discard(sandbox.id)
        self._claims.discard(sandbox.id)
        await self._delete_objects(sandbox.id)

    async def _delete_objects(self, name: str) -> None:
        await self._api.delete_object(
            group=CILIUM_GROUP,
            version=CILIUM_VERSION,
            plural=CILIUM_PLURAL,
            namespace=self.namespace,
            name=f"{name}-egress",
        )
        await self._api.delete_object(
            group=EXTENSIONS_GROUP,
            version=API_VERSION,
            plural=SANDBOX_CLAIM_PLURAL,
            namespace=self.namespace,
            name=name,
        )
        await self._api.delete_object(
            group=SANDBOX_GROUP,
            version=API_VERSION,
            plural=SANDBOX_PLURAL,
            namespace=self.namespace,
            name=name,
        )

    # -- object bodies -------------------------------------------------------------

    def _sandbox_body(
        self,
        name: str,
        spec: SandboxSpec,
        labels: Mapping[str, str],
    ) -> dict[str, Any]:
        shutdown = datetime.fromtimestamp(self._clock(), tz=UTC) + timedelta(
            seconds=spec.max_lifetime_s
        )
        return {
            "apiVersion": f"{SANDBOX_GROUP}/{API_VERSION}",
            "kind": "Sandbox",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": dict(labels),
                "annotations": {
                    "spleef.io/template-digest": spec.template.digest,
                    "spleef.io/role": spec.role,
                },
            },
            "spec": {
                "shutdownPolicy": "Delete",
                "shutdownTime": shutdown.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "podTemplate": {
                    "metadata": {"labels": dict(labels)},
                    "spec": self._pod_spec(spec),
                },
            },
        }

    def _pod_spec(self, spec: SandboxSpec) -> dict[str, Any]:
        run_as_user = spec.run_as_user
        security_context: dict[str, Any] = {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        if run_as_user.isdigit():
            security_context["runAsUser"] = int(run_as_user)
        cpu = _cpu_quantity(spec.resources.cpus)
        memory = f"{spec.resources.memory_mb}Mi"
        disk = f"{spec.resources.disk_mb}Mi"
        container = {
            "name": CONTAINER_NAME,
            "image": _image_reference(spec),
            "command": ["sleep", "infinity"],
            "workingDir": "/workspace",
            "env": [{"name": key, "value": value} for key, value in spec.env.items()],
            "resources": {
                "requests": {"cpu": cpu, "memory": memory, "ephemeral-storage": disk},
                "limits": {"cpu": cpu, "memory": memory, "ephemeral-storage": disk},
            },
            "securityContext": {
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
        return {
            "runtimeClassName": self.runtime_class,
            "automountServiceAccountToken": False,
            "hostname": "sandbox",
            "restartPolicy": "Never",
            "nodeSelector": {"kubernetes.io/arch": spec.arch.value},
            "securityContext": security_context,
            "containers": [container],
            "volumes": [
                {"name": "workspace", "emptyDir": {}},
                {"name": "tmp", "emptyDir": {}},
            ],
        }

    async def _create_claim(
        self,
        name: str,
        spec: SandboxSpec,
        labels: Mapping[str, str],
    ) -> None:
        if self._warm_pool is None:
            raise SandboxCreateError("warm_pool must be configured before claiming")
        shutdown = datetime.fromtimestamp(self._clock(), tz=UTC) + timedelta(
            seconds=spec.max_lifetime_s
        )
        body = {
            "apiVersion": f"{EXTENSIONS_GROUP}/{API_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": dict(labels),
                "annotations": {"spleef.io/template-digest": spec.template.digest},
            },
            "spec": {
                "warmPoolRef": {"name": self._warm_pool},
                "lifecycle": {
                    "shutdownPolicy": "Delete",
                    "shutdownTime": shutdown.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
        }
        await self._api.create_object(
            group=EXTENSIONS_GROUP,
            version=API_VERSION,
            plural=SANDBOX_CLAIM_PLURAL,
            namespace=self.namespace,
            body=body,
        )

    async def _wait_ready(self, name: str) -> None:
        deadline = time.monotonic() + self._ready_timeout_s
        while True:
            body = await self._api.get_object(
                group=SANDBOX_GROUP,
                version=API_VERSION,
                plural=SANDBOX_PLURAL,
                namespace=self.namespace,
                name=name,
            )
            if body is not None and _is_ready(body):
                return
            if time.monotonic() >= deadline:
                raise SandboxCreateError(
                    f"sandbox {name!r} was not ready within {self._ready_timeout_s}s"
                )
            await asyncio.sleep(self._ready_poll_s)

    # -- validation ----------------------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> None:
        caps = self.capabilities()
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(f"k8s agent-sandbox supports linux only, got {spec.os!r}")
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(f"unsupported architecture {spec.arch!r}")
        if spec.template.kind != "oci":
            raise CapabilityNotSupported(
                f"k8s agent-sandbox needs an oci template, got {spec.template.kind!r}"
            )
        if spec.gui:
            raise CapabilityNotSupported("k8s agent-sandbox does not support GUI sandboxes")
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("k8s agent-sandbox does not reserve GPUs")
        if spec.max_lifetime_s > self._max_lifetime_s:
            raise CapabilityNotSupported(
                f"max_lifetime_s {spec.max_lifetime_s} exceeds {self._max_lifetime_s}"
            )
        policy = spec.network
        if policy.mode not in caps.net_modes:
            raise PolicyNotEnforceable(f"network mode {policy.mode.value!r} is not supported")
        if policy.allow_cidrs:
            raise PolicyNotEnforceable(
                "k8s agent-sandbox enforces hostname allowlists only; cidr allowlists "
                "are not generated"
            )
        if policy.via_proxy is not None:
            raise PolicyNotEnforceable(
                "via_proxy is not supported: egress is restricted by a Cilium FQDN policy"
            )
        if policy.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
            for host in policy.allow_hosts:
                if not _HOST_RE.match(host):
                    raise PolicyNotEnforceable(f"invalid allowlist hostname: {host!r}")
        elif policy.allow_hosts:
            raise PolicyNotEnforceable(
                f"host allowlists are not meaningful for network mode {policy.mode.value!r}"
            )

    def _labels(self, spec: SandboxSpec) -> dict[str, str]:
        labels = dict(spec.labels)
        now = int(self._clock())
        labels["spleef.provider"] = _label_value(self.instance)
        labels["spleef.created_at"] = str(now)
        labels["spleef.expires_at"] = str(now + spec.max_lifetime_s)
        for key, value in labels.items():
            _validate_label(key, value)
        return labels


def _isolation_for(runtime_class: str) -> Isolation:
    if runtime_class in _GVISOR_CLASSES:
        return Isolation.L2
    if runtime_class.startswith(_KATA_PREFIX):
        return Isolation.L3
    raise ValueError(
        f"unsupported runtime class {runtime_class!r}: use gvisor/runsc or Kata (kata-*)"
    )


def _image_reference(spec: SandboxSpec) -> str:
    digest = spec.template.digest
    if digest.startswith("sha256:") and set(digest[7:]) != {"0"}:
        return f"{spec.template.ref}@{digest}"
    return spec.template.ref


def _cpu_quantity(cpus: float) -> str:
    if cpus == int(cpus):
        return str(int(cpus))
    return f"{round(cpus * 1000)}m"


def _is_ready(body: Mapping[str, Any]) -> bool:
    status = body.get("status", {})
    for condition in status.get("conditions", []):
        if condition.get("type") == "Ready" and str(condition.get("status")) == "True":
            return True
    return False


def _validate_label(key: str, value: str) -> None:
    prefix: str | None
    if "/" in key:
        prefix, name = key.rsplit("/", 1)
    else:
        prefix, name = None, key
    if not name or len(name) > 63 or not _LABEL_NAME_RE.match(name):
        raise SandboxCreateError(f"invalid kubernetes label key: {key!r}")
    if prefix is not None and (len(prefix) > 253 or not _DNS_PREFIX_RE.match(prefix)):
        raise SandboxCreateError(f"invalid kubernetes label prefix in {key!r}")
    if len(value) > 63 or (value and not _LABEL_NAME_RE.match(value)):
        raise SandboxCreateError(f"invalid kubernetes label value for {key!r}: {value!r}")


def _label_value(value: str) -> str:
    """Shorten an instance name to a legal label value."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    return cleaned[:63].strip("-._") or "unknown"


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    return int(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
