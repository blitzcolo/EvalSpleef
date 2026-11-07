"""VpcVllmProvider: self-hosted vLLM or SGLang on Kubernetes or GPU instances (TECH-DESIGN 6.6.4).

API management mode: deploy, status, scale, teardown, wake, upstream are programmatically
orchestrated against either a Kubernetes cluster (Deployment + Service) or rented GPU instances
(Novita GPU Instance REST API).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

import httpx

from evalspleef.spi.errors import CapabilityNotSupported, EndpointUnavailable
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.secrets import SecretResolver

from .clock import Clock, SystemClock
from .engine_args import build_engine_args
from .gpu_instance import GpuInstanceOrchestrator, NovitaGpuInstanceClient
from .inference import (
    DEFAULT_NOT_READY_MARKERS,
    DEFAULT_PROBE_TIMEOUT_S,
    DEFAULT_WAKE_POLL_S,
    probe,
    raise_for_probe,
    wake_loop,
)
from .k8s import InMemoryKubeApi, KubeApi, KubeOrchestrator, KubernetesClientKubeApi

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
DEFAULT_PORT = 8000


class VpcVllmProvider(ModelEndpointProvider):
    name: str = "vpc-vllm"
    management_mode: ManagementMode = ManagementMode.API

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
        kube_api: KubeApi | None = None,
        gpu_transport: httpx.AsyncBaseTransport | None = None,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._clock = clock or SystemClock()
        self.kube_api = kube_api
        self.gpu_transport = gpu_transport
        self._probe_transport = probe_transport
        self.hf_checker: HFAccessChecker | None = None

    def _get_kube_api(self) -> KubeApi:
        if self.kube_api is not None:
            return self.kube_api
        use_fake = bool(self.config.get("fake", False) or self.config.get("in_memory_k8s", False))
        if use_fake:
            self.kube_api = InMemoryKubeApi()
            return self.kube_api
        kubeconfig = self.config.get("kubeconfig")
        context = self.config.get("kube_context")
        self.kube_api = KubernetesClientKubeApi(kubeconfig=kubeconfig, context=context)
        return self.kube_api

    def _get_gpu_client(self) -> NovitaGpuInstanceClient:
        api_key_ref = str(self.config.get("api_key_ref") or "")

        def _resolve_key() -> str:
            if not api_key_ref:
                return ""
            try:
                return self.secrets.resolve(api_key_ref)
            except Exception:
                return ""

        api_base = str(self.config.get("api_base") or "https://api.novita.ai")
        return NovitaGpuInstanceClient(
            _resolve_key,
            api_base=api_base,
            transport=self.gpu_transport,
        )

    # ----------------------------------------------------------------------------------
    # SPI methods
    # ----------------------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        checks: list[tuple[str, bool, str]] = []

        # 1. Hugging Face source preflight
        if not isinstance(spec.source, HFSource):
            checks.append(
                ("hf_source", False, "vpc-vllm deployments require a Hugging Face source")
            )
            return ValidationReport(ok=False, checks=tuple(checks))

        if self.hf_checker is not None:
            hf_report = await self.hf_checker.preflight(spec.source)
            checks.extend(hf_report.checks)
        else:
            checks.append(("hf_source", True, f"repo {spec.source.repo}"))
            valid_rev = bool(_SHA.match(spec.source.revision))
            checks.append(
                (
                    "revision_pinned",
                    valid_rev,
                    "40-hex commit sha"
                    if valid_rev
                    else "revision must be a 40-character commit sha",
                )
            )

        # 2. Pinned image digest check
        image = str(
            spec.deployment.params.get("image")
            or self.config.get("image")
            or "vllm/vllm-openai@sha256:" + "0" * 64
        )
        pinned = bool(_DIGEST_PINNED.search(image))
        checks.append(
            (
                "image_pinned",
                pinned,
                "image is pinned by sha256 digest"
                if pinned
                else "image must be pinned with @sha256:...",
            )
        )

        return ValidationReport(
            ok=all(passed for _id, passed, _msg in checks), checks=tuple(checks)
        )

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        params = spec.deployment.params
        gpu_count = params.get("gpu_count", 1)
        gpu_type = str(params["gpu_type"]) if "gpu_type" in params else None
        replicas_min = int(params.get("min_replicas", 1))
        replicas_max = int(params.get("max_replicas", 1))
        max_concurrency = int(params.get("max_concurrency", 16))
        est_vram_gb = float(params.get("est_vram_gb", 24.0))
        est_cost_per_hour_usd = float(params.get("est_cost_per_hour_usd", 1.50))

        return DeploymentPlan(
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            replicas_min=replicas_min,
            replicas_max=replicas_max,
            max_concurrency=max_concurrency,
            est_vram_gb=est_vram_gb,
            est_cost_per_hour_usd=est_cost_per_hour_usd,
        )

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        endpoint_id = f"ep_{spec.slug}_{hashlib.sha256(f'{spec.slug}-{spec.spec_version}'.encode()).hexdigest()[:8]}"
        image = str(
            spec.deployment.params.get("image")
            or self.config.get("image")
            or "vllm/vllm-openai@sha256:" + "0" * 64
        )
        port = int(spec.deployment.params.get("port") or self.config.get("port") or DEFAULT_PORT)

        # Resolve HF token from secrets; never log this token!
        hf_token: str | None = None
        if isinstance(spec.source, HFSource) and spec.source.token_ref:
            hf_token = self.secrets.resolve(spec.source.token_ref)

        engine_args = build_engine_args(
            spec,
            served_model_name=spec.slug,
            tensor_parallel_size=plan.gpu_count or 1,
            port=port,
        )

        backend = str(
            spec.deployment.params.get("backend") or self.config.get("backend") or "k8s"
        ).lower()

        external_ids: dict[str, str] = {"backend": backend}
        base_url: str

        if backend in ("k8s", "kubernetes"):
            kube_api = self._get_kube_api()
            namespace = str(
                spec.deployment.params.get("namespace") or self.config.get("namespace") or "default"
            )
            url_template = spec.deployment.params.get("url_template") or self.config.get(
                "url_template"
            )
            orchestrator = KubeOrchestrator(
                kube_api, namespace=namespace, url_template=url_template
            )
            name, service_url = await orchestrator.deploy(
                endpoint_id=endpoint_id,
                image=image,
                args=engine_args,
                replicas=plan.replicas_min,
                gpu_count=plan.gpu_count,
                hf_token=hf_token,
                extra_env=spec.deployment.params.get("extra_env"),
                port=port,
            )
            external_ids["name"] = name
            external_ids["namespace"] = namespace
            base_url = service_url

        elif backend in ("gpu_instance", "novita_gpu_instance", "instance"):
            client = self._get_gpu_client()
            url_template = spec.deployment.params.get("url_template") or self.config.get(
                "url_template"
            )
            gpu_orchestrator = GpuInstanceOrchestrator(client, url_template=url_template)
            instance_id, initial_url = await gpu_orchestrator.deploy(
                endpoint_id=endpoint_id,
                image=image,
                args=engine_args,
                gpu_type=plan.gpu_type,
                gpu_count=plan.gpu_count,
                hf_token=hf_token,
                extra_env=spec.deployment.params.get("extra_env"),
                port=port,
            )
            external_ids["instance_id"] = instance_id
            try:
                _inst_state, fetched_url = await gpu_orchestrator.status(instance_id)
                base_url = fetched_url or initial_url or f"http://{instance_id}:{port}/v1"
            except Exception:
                base_url = initial_url or f"http://{instance_id}:{port}/v1"

        else:
            raise CapabilityNotSupported(f"unsupported orchestration backend {backend!r}")

        # Explicit base_url override for testing and conformance environments
        if "base_url" in spec.deployment.params:
            base_url = str(spec.deployment.params["base_url"])
        elif "base_url" in self.config:
            base_url = str(self.config["base_url"])

        handle = EndpointHandle(
            endpoint_id=endpoint_id,
            model=spec,
            state=EndpointState.READY,
            base_url=base_url,
            upstream_model=spec.slug,
            protocol="chat_completions",
            credential_ref=str(self.config.get("api_key_ref") or ""),
            external_ids=external_ids,
        )
        return handle

    async def status(self, handle: EndpointHandle) -> EndpointState:
        backend = handle.external_ids.get("backend", "k8s")
        if backend in ("k8s", "kubernetes"):
            name = handle.external_ids["name"]
            namespace = handle.external_ids.get("namespace", "default")
            k8s_orch = KubeOrchestrator(self._get_kube_api(), namespace=namespace)
            k8s_state = await k8s_orch.status(name)
            handle.state = k8s_state
            return k8s_state

        if backend in ("gpu_instance", "novita_gpu_instance", "instance"):
            instance_id = handle.external_ids["instance_id"]
            client = self._get_gpu_client()
            gpu_orch = GpuInstanceOrchestrator(client)
            gpu_state, url = await gpu_orch.status(instance_id)
            if url and handle.base_url is None:
                handle.base_url = url
            handle.state = gpu_state
            return gpu_state

        raise CapabilityNotSupported(f"unsupported orchestration backend {backend!r}")

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        backend = handle.external_ids.get("backend", "k8s")
        if backend in ("k8s", "kubernetes"):
            name = handle.external_ids["name"]
            namespace = handle.external_ids.get("namespace", "default")
            k8s_orch = KubeOrchestrator(self._get_kube_api(), namespace=namespace)
            await k8s_orch.scale(name, min_replicas, max_replicas)
            return

        if backend in ("gpu_instance", "novita_gpu_instance", "instance"):
            instance_id = handle.external_ids["instance_id"]
            client = self._get_gpu_client()
            gpu_orch = GpuInstanceOrchestrator(client)
            await gpu_orch.scale(instance_id, min_replicas, max_replicas)
            return

        raise CapabilityNotSupported(f"unsupported orchestration backend {backend!r}")

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        backend = handle.external_ids.get("backend", "k8s")
        if backend in ("k8s", "kubernetes"):
            name = handle.external_ids["name"]
            namespace = handle.external_ids.get("namespace", "default")
            k8s_orch = KubeOrchestrator(self._get_kube_api(), namespace=namespace)
            state = await k8s_orch.teardown(name, mode)
            handle.state = state
            return

        if backend in ("gpu_instance", "novita_gpu_instance", "instance"):
            instance_id = handle.external_ids["instance_id"]
            client = self._get_gpu_client()
            gpu_orch = GpuInstanceOrchestrator(client)
            gpu_state = await gpu_orch.teardown(instance_id, mode)
            handle.state = gpu_state
            return

        raise CapabilityNotSupported(f"unsupported orchestration backend {backend!r}")

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        if handle.base_url is None:
            await self.status(handle)
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base URL")

        api_key = ""
        if handle.credential_ref:
            try:
                api_key = self.secrets.resolve(handle.credential_ref)
            except Exception:
                api_key = ""

        poll_s = float(self.config.get("wake_poll_interval_s", DEFAULT_WAKE_POLL_S))
        probe_cap_s = float(self.config.get("status_timeout_s", DEFAULT_PROBE_TIMEOUT_S))

        async def _probe_once(cap_s: float) -> Any:
            return await probe(
                api_base=handle.base_url or "",
                api_key=api_key,
                model=handle.upstream_model or handle.model.slug,
                clock=self._clock,
                timeout_s=cap_s,
                not_ready_markers=DEFAULT_NOT_READY_MARKERS,
                transport=self._probe_transport,
            )

        result, elapsed = await wake_loop(
            _probe_once,
            clock=self._clock,
            timeout_s=float(timeout_s),
            poll_s=poll_s,
            probe_cap_s=probe_cap_s,
        )

        if result.outcome == "ok":
            handle.observed["last_wake_s"] = elapsed
            handle.state = EndpointState.WARM
        elif result.outcome in ("not_ready", "timeout"):
            handle.state = EndpointState.SLEEPING
        else:
            raise_for_probe(result, "wake")

        return handle.state

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        api_base = handle.base_url or "http://service:8000/v1"
        return UpstreamRoute(
            api_base=api_base,
            model=handle.upstream_model or handle.model.slug,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            cold_start_window_s=int(self.config.get("cold_start_window_s", 0)),
        )
