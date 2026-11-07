"""Kubernetes orchestration backend for self-hosted vLLM/SGLang deployments.

Renders Kubernetes manifests as plain dicts and applies them through a KubeApi protocol.
Supports both a lazily-imported official kubernetes client and an in-memory fake.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Protocol

from evalspleef.spi.model import EndpointState, TeardownMode

from .errors import KubeApiError

DEFAULT_PORT = 8000
DEFAULT_NAMESPACE = "default"


class KubeApi(Protocol):
    """Protocol for Kubernetes API operations."""

    async def create_deployment(self, namespace: str, manifest: dict[str, Any]) -> None: ...

    async def get_deployment(self, namespace: str, name: str) -> dict[str, Any] | None: ...

    async def patch_deployment_scale(self, namespace: str, name: str, replicas: int) -> None: ...

    async def delete_deployment(self, namespace: str, name: str) -> None: ...

    async def create_service(self, namespace: str, manifest: dict[str, Any]) -> None: ...

    async def get_service(self, namespace: str, name: str) -> dict[str, Any] | None: ...

    async def delete_service(self, namespace: str, name: str) -> None: ...


class InMemoryKubeApi:
    """In-memory test double for KubeApi."""

    def __init__(self) -> None:
        self.deployments: dict[tuple[str, str], dict[str, Any]] = {}
        self.services: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, str, Any]] = []

    async def create_deployment(self, namespace: str, manifest: dict[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        self.calls.append(("create_deployment", namespace, name, manifest))
        record = deepcopy(manifest)
        spec = record.get("spec", {})
        replicas = int(spec.get("replicas", 1))
        record.setdefault(
            "status",
            {
                "replicas": replicas,
                "readyReplicas": replicas,
                "availableReplicas": replicas,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        )
        self.deployments[(namespace, name)] = record

    async def get_deployment(self, namespace: str, name: str) -> dict[str, Any] | None:
        self.calls.append(("get_deployment", namespace, name, None))
        record = self.deployments.get((namespace, name))
        return deepcopy(record) if record is not None else None

    async def patch_deployment_scale(self, namespace: str, name: str, replicas: int) -> None:
        self.calls.append(("patch_deployment_scale", namespace, name, replicas))
        record = self.deployments.get((namespace, name))
        if record is None:
            raise KubeApiError(f"Deployment {name!r} not found in namespace {namespace!r}")
        record.setdefault("spec", {})["replicas"] = replicas
        record.setdefault("status", {})
        record["status"]["replicas"] = replicas
        record["status"]["readyReplicas"] = replicas
        record["status"]["availableReplicas"] = replicas

    async def delete_deployment(self, namespace: str, name: str) -> None:
        self.calls.append(("delete_deployment", namespace, name, None))
        self.deployments.pop((namespace, name), None)

    async def create_service(self, namespace: str, manifest: dict[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        self.calls.append(("create_service", namespace, name, manifest))
        self.services[(namespace, name)] = deepcopy(manifest)

    async def get_service(self, namespace: str, name: str) -> dict[str, Any] | None:
        self.calls.append(("get_service", namespace, name, None))
        record = self.services.get((namespace, name))
        return deepcopy(record) if record is not None else None

    async def delete_service(self, namespace: str, name: str) -> None:
        self.calls.append(("delete_service", namespace, name, None))
        self.services.pop((namespace, name), None)


class KubernetesClientKubeApi:
    """KubeApi implementation using the official kubernetes Python client.

    Lazily imports the ``kubernetes`` package so environments without it can still use
    the adapter with InMemoryKubeApi.
    """

    def __init__(self, kubeconfig: str | None = None, context: str | None = None) -> None:
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise KubeApiError(
                "The 'kubernetes' package is required for KubernetesClientKubeApi. "
                "Install it with evalspleef-model-vpc-vllm[k8s]."
            ) from exc

        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig, context=context)  # type: ignore[no-untyped-call]
        else:
            try:
                config.load_incluster_config()  # type: ignore[no-untyped-call]
            except config.ConfigException:
                config.load_kube_config(context=context)  # type: ignore[no-untyped-call]

        self._apps_v1 = client.AppsV1Api()
        self._core_v1 = client.CoreV1Api()

    async def create_deployment(self, namespace: str, manifest: dict[str, Any]) -> None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> None:
            self._apps_v1.create_namespaced_deployment(
                namespace=namespace,
                body=manifest,  # type: ignore[arg-type]
            )

        try:
            await asyncio.to_thread(_call)
        except ApiException as exc:
            raise KubeApiError(f"Failed to create deployment: {exc.reason}") from exc

    async def get_deployment(self, namespace: str, name: str) -> dict[str, Any] | None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> Any:
            try:
                return self._apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return None
                raise

        try:
            dep = await asyncio.to_thread(_call)
            return dep.to_dict() if dep is not None else None
        except ApiException as exc:
            raise KubeApiError(f"Failed to get deployment {name}: {exc.reason}") from exc

    async def patch_deployment_scale(self, namespace: str, name: str, replicas: int) -> None:
        from kubernetes.client.exceptions import ApiException

        body = {"spec": {"replicas": replicas}}

        def _call() -> None:
            self._apps_v1.patch_namespaced_deployment_scale(
                name=name, namespace=namespace, body=body
            )

        try:
            await asyncio.to_thread(_call)
        except ApiException as exc:
            raise KubeApiError(f"Failed to scale deployment {name}: {exc.reason}") from exc

    async def delete_deployment(self, namespace: str, name: str) -> None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> None:
            try:
                self._apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise

        try:
            await asyncio.to_thread(_call)
        except ApiException as exc:
            raise KubeApiError(f"Failed to delete deployment {name}: {exc.reason}") from exc

    async def create_service(self, namespace: str, manifest: dict[str, Any]) -> None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> None:
            self._core_v1.create_namespaced_service(
                namespace=namespace,
                body=manifest,  # type: ignore[arg-type]
            )

        try:
            await asyncio.to_thread(_call)
        except ApiException as exc:
            raise KubeApiError(f"Failed to create service: {exc.reason}") from exc

    async def get_service(self, namespace: str, name: str) -> dict[str, Any] | None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> Any:
            try:
                return self._core_v1.read_namespaced_service(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return None
                raise

        try:
            svc = await asyncio.to_thread(_call)
            return svc.to_dict() if svc is not None else None
        except ApiException as exc:
            raise KubeApiError(f"Failed to get service {name}: {exc.reason}") from exc

    async def delete_service(self, namespace: str, name: str) -> None:
        from kubernetes.client.exceptions import ApiException

        def _call() -> None:
            try:
                self._core_v1.delete_namespaced_service(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise

        try:
            await asyncio.to_thread(_call)
        except ApiException as exc:
            raise KubeApiError(f"Failed to delete service {name}: {exc.reason}") from exc


def build_deployment_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    args: list[str],
    replicas: int = 1,
    gpu_count: int | None = 1,
    hf_token: str | None = None,
    labels: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Render a Kubernetes Deployment manifest as a plain dictionary."""
    app_labels = dict(labels or {"app": "vllm", "evalspleef.io/endpoint": name})
    env_list: list[dict[str, str]] = []
    if hf_token:
        env_list.append({"name": "HUGGING_FACE_HUB_TOKEN", "value": hf_token})
    if extra_env:
        for k, v in sorted(extra_env.items()):
            env_list.append({"name": k, "value": v})

    resources: dict[str, Any] = {}
    if gpu_count is not None and gpu_count > 0:
        gpu_str = str(gpu_count)
        resources = {
            "limits": {"nvidia.com/gpu": gpu_str},
            "requests": {"nvidia.com/gpu": gpu_str},
        }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": app_labels,
        },
        "spec": {
            "replicas": replicas,
            "selector": {
                "matchLabels": app_labels,
            },
            "template": {
                "metadata": {
                    "labels": app_labels,
                },
                "spec": {
                    "containers": [
                        {
                            "name": "vllm",
                            "image": image,
                            "args": list(args),
                            "ports": [{"containerPort": port, "name": "http"}],
                            "env": env_list,
                            "resources": resources,
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": port},
                                "initialDelaySeconds": 15,
                                "periodSeconds": 10,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": port},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 15,
                            },
                        }
                    ]
                },
            },
        },
    }


def build_service_manifest(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str] | None = None,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Render a Kubernetes Service manifest as a plain dictionary."""
    app_labels = dict(labels or {"app": "vllm", "evalspleef.io/endpoint": name})
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": app_labels,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": app_labels,
            "ports": [
                {
                    "name": "http",
                    "port": port,
                    "targetPort": port,
                }
            ],
        },
    }


def map_deployment_status(deployment: dict[str, Any] | None) -> EndpointState:
    """Map a Kubernetes Deployment resource dict to an EndpointState."""
    if deployment is None:
        return EndpointState.RETIRED
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    desired_replicas = int(spec.get("replicas", 1))
    if desired_replicas == 0:
        return EndpointState.SLEEPING

    ready_replicas = int(status.get("readyReplicas", 0))
    available_replicas = int(status.get("availableReplicas", 0))

    conditions = status.get("conditions") or []
    for cond in conditions:
        if (
            isinstance(cond, dict)
            and cond.get("type") == "ReplicaFailure"
            and cond.get("status") == "True"
        ):
            return EndpointState.FAILED

    if ready_replicas >= desired_replicas and available_replicas >= desired_replicas:
        return EndpointState.READY
    return EndpointState.DEPLOYING


class KubeOrchestrator:
    """Kubernetes orchestrator for vLLM endpoints."""

    def __init__(
        self,
        kube_api: KubeApi,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        cluster_domain: str = "cluster.local",
        url_template: str | None = None,
    ) -> None:
        self.api = kube_api
        self.namespace = namespace
        self.cluster_domain = cluster_domain
        self.url_template = url_template

    def service_name(self, endpoint_id: str) -> str:
        # Lowercase alphanumeric + hyphen, <= 63 chars
        safe = endpoint_id.lower().replace("_", "-")
        return f"vllm-{safe}"[:63].rstrip("-")

    def service_url(self, name: str, port: int = DEFAULT_PORT) -> str:
        if self.url_template:
            return self.url_template.format(
                name=name, namespace=self.namespace, domain=self.cluster_domain, port=port
            )
        return f"http://{name}:8000/v1"

    async def deploy(
        self,
        *,
        endpoint_id: str,
        image: str,
        args: list[str],
        replicas: int,
        gpu_count: int | None,
        hf_token: str | None,
        extra_env: dict[str, str] | None = None,
        port: int = DEFAULT_PORT,
    ) -> tuple[str, str]:
        """Create Deployment and Service. Returns (name, base_url)."""
        name = self.service_name(endpoint_id)
        dep_manifest = build_deployment_manifest(
            name=name,
            namespace=self.namespace,
            image=image,
            args=args,
            replicas=replicas,
            gpu_count=gpu_count,
            hf_token=hf_token,
            extra_env=extra_env,
            port=port,
        )
        svc_manifest = build_service_manifest(
            name=name,
            namespace=self.namespace,
            port=port,
        )
        await self.api.create_deployment(self.namespace, dep_manifest)
        await self.api.create_service(self.namespace, svc_manifest)
        return name, self.service_url(name, port)

    async def status(self, name: str) -> EndpointState:
        dep = await self.api.get_deployment(self.namespace, name)
        return map_deployment_status(dep)

    async def scale(self, name: str, min_replicas: int, max_replicas: int) -> None:
        target = max(min_replicas, 0)
        await self.api.patch_deployment_scale(self.namespace, name, target)

    async def teardown(self, name: str, mode: TeardownMode) -> EndpointState:
        if mode == "sleep":
            await self.api.patch_deployment_scale(self.namespace, name, 0)
            return EndpointState.SLEEPING
        await self.api.delete_service(self.namespace, name)
        await self.api.delete_deployment(self.namespace, name)
        return EndpointState.RETIRED
