"""``novita-serverless-gpu``: vLLM on Novita Serverless GPUs v2, api mode (section 6.6.3).

The lifecycle is fully automatic: ``deploy`` creates a sync endpoint that runs a digest-pinned
vLLM image against a pinned model revision, ``status`` maps the REST status, ``scale`` changes
the worker config and ``teardown`` deletes the endpoint. Engine arguments come from the
per-family profiles, so revision, engine and flags are all recorded.

Everything here is *not verified against a real Novita account*.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shlex
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
from .engine_args import Engine, build_engine_args, select_profile
from .errors import CredentialRejected, DeploymentNotFound, PlanInputMissing
from .gpu_api import DEFAULT_API_BASE, NovitaGpuClient
from .inference import (
    DEFAULT_NOT_READY_MARKERS,
    ProbeResult,
    probe,
    raise_for_probe,
    wake_loop,
)

HF_TOKEN_ENV = "HUGGING_FACE_HUB_TOKEN"
DEFAULT_PORT = 8000
DEFAULT_HEALTH_PATH = "/health"
DEFAULT_MAX_CONCURRENT = 16
DEFAULT_REQUEST_TIMEOUT_S = 600
DEFAULT_IDLE_TIMEOUT_S = 300
DEFAULT_SCALE_POLICY: dict[str, Any] = {"type": "queue", "value": 60}
DEFAULT_WAKE_POLL_S = 15.0
DEFAULT_PROBE_TIMEOUT_S = 90.0

_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SERVING = frozenset({"serving", "running", "ready", "active", "healthy"})
_STARTING = frozenset(
    {"creating", "pending", "deploying", "starting", "initializing", "scaling", "updating"}
    | {"pulling", "provisioning", "rolling"}
)
_FAILED = frozenset({"failed", "error", "abnormal", "unhealthy"})
_GONE = frozenset({"deleted", "terminated", "terminating", "deleting"})
_IDLE = frozenset({"sleeping", "stopped", "idle", "paused"})
_WORKER_UP = frozenset({"ready", "running", "serving"})
_WORKER_KEYS = (
    "min_replicas",
    "max_replicas",
    "idle_timeout",
    "max_concurrent_per_worker",
    "gpu_num",
    "rootfs_size_gb",
    "min_cuda_version",
    "request_timeout",
)


def map_status(payload: Mapping[str, Any]) -> tuple[EndpointState, str | None]:
    """Map a GET endpoint answer to ``(state, unrecognized status or None)``.

    The documented examples show ``running`` (endpoint) and ``ready`` (worker); the full value
    set is not documented, so an unknown value yields ``DEGRADED`` and is reported to the caller.
    """
    status_obj = payload.get("status")
    status_obj = status_obj if isinstance(status_obj, Mapping) else {}
    value = str(status_obj.get("status") or "").lower()
    error = str(status_obj.get("error") or "")
    if value in _GONE:
        return EndpointState.RETIRED, None
    if value in _FAILED or (error and value not in _SERVING):
        return EndpointState.FAILED, None
    if value in _STARTING:
        return EndpointState.DEPLOYING, None
    if value in _IDLE:
        return EndpointState.SLEEPING, None
    if value in _SERVING:
        workers = payload.get("workers")
        up = [
            worker
            for worker in (workers if isinstance(workers, list) else [])
            if isinstance(worker, Mapping)
            and str((worker.get("status") or {}).get("status", "")).lower() in _WORKER_UP
            and worker.get("health") is not False
            and not worker.get("drain")
        ]
        if up:
            return EndpointState.READY, None
        config = payload.get("worker_config")
        minimum = config.get("min_replicas") if isinstance(config, Mapping) else None
        return (EndpointState.SLEEPING if minimum == 0 else EndpointState.DEPLOYING), None
    return EndpointState.DEGRADED, value or "<empty>"


def _endpoint_id(spec: ModelSpec) -> str:
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


class NovitaServerlessGpuProvider(ModelEndpointProvider):
    """Api-mode provider for vLLM workers on Novita Serverless GPUs."""

    name = "novita-serverless-gpu"
    management_mode = ManagementMode.API
    hf_checker: HFAccessChecker | None = None
    """Assigned by the control plane after construction; ``validate`` fails without it."""

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._transport = transport
        self._api_transport = api_transport
        self._hidden: list[str] = []

    # -- configuration ---------------------------------------------------------------------

    def _credential_ref(self, spec: ModelSpec | None = None) -> str:
        values = [self.config.get("api_key_ref")]
        if spec is not None:
            values.insert(0, spec.deployment.params.get("api_key_ref"))
        for value in values:
            if isinstance(value, str) and value:
                return value
        suffix = re.sub(r"[^A-Z0-9]", "_", self.account.upper())
        return f"env://NOVITA_API_KEY_{suffix}"

    def _client(self, credential_ref: str) -> NovitaGpuClient:
        return NovitaGpuClient(
            lambda: self._resolve(credential_ref),
            api_base=str(self.config.get("api_base", DEFAULT_API_BASE)),
            transport=self._api_transport,
            hidden=lambda: tuple(self._hidden),
        )

    def _resolve(self, ref: str) -> str:
        try:
            return self.secrets.resolve(ref)
        except (KeyError, ValueError) as exc:
            raise CredentialRejected(
                f"the secret reference {ref} does not resolve",
                detail={"reason": "secret_unresolved"},
            ) from exc

    def _param(self, spec: ModelSpec, key: str, default: Any = None) -> Any:
        value = spec.deployment.params.get(key)
        return value if value is not None else self.config.get(key, default)

    def _policy(self, spec: ModelSpec) -> dict[str, Any]:
        raw = self._param(spec, "scale_policy") or {}
        policy = dict(DEFAULT_SCALE_POLICY)
        if isinstance(raw, Mapping):
            policy.update({key: raw[key] for key in ("type", "value") if key in raw})
        if policy["type"] not in ("queue", "concurrency"):
            raise CapabilityNotSupported(
                f"scale policy {policy['type']!r} is not supported (queue, concurrency)",
                detail={"scale_policy": policy},
            )
        return {"type": str(policy["type"]), "value": int(policy["value"])}

    def _engine(self, spec: ModelSpec) -> Engine:
        engine = str(self._param(spec, "engine", "vllm"))
        if engine not in ("vllm", "sglang"):
            raise CapabilityNotSupported(
                f"serverless engine {engine!r} is not supported (vllm, sglang)",
                detail={"engine": engine},
            )
        return "sglang" if engine == "sglang" else "vllm"

    def _markers(self) -> tuple[str, ...]:
        raw = self.config.get("not_ready_markers")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(marker).lower() for marker in raw)
        return DEFAULT_NOT_READY_MARKERS

    # -- validate / plan -------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        checks: list[tuple[str, bool, str]] = []
        credential_ref = self._credential_ref(spec)
        try:
            resolved = bool(self.secrets.resolve(credential_ref))
            detail = credential_ref if resolved else f"{credential_ref} resolves to an empty value"
        except (KeyError, ValueError) as exc:
            resolved, detail = False, f"{credential_ref} does not resolve ({type(exc).__name__})"
        checks.append(("novita_key", resolved, detail))

        image = str(self.config.get("image", ""))
        checks.append(
            (
                "image_digest_pinned",
                bool(_DIGEST_PINNED.search(image)),
                f"image {image!r} must end in @sha256:<64 hex>",
            )
        )
        product_id = self._param(spec, "product_id")
        checks.append(("product_id", bool(product_id), "Novita serverless product id (GPU spec)"))

        source = spec.source
        if not isinstance(source, HFSource):
            checks.append(("model_source", False, "vLLM deployment needs a Hugging Face source"))
            return ValidationReport(ok=False, checks=tuple(checks))
        checks.append(
            (
                "revision_pinned",
                bool(_SHA.match(source.revision)),
                f"revision {source.revision!r} must be a 40-hex commit sha",
            )
        )
        if source.access != "public":
            token_ok = False
            token_detail = "non-public repository without token_ref"
            if source.token_ref:
                try:
                    token_ok = bool(self.secrets.resolve(source.token_ref))
                    token_detail = source.token_ref
                except (KeyError, ValueError):
                    token_detail = f"{source.token_ref} does not resolve"
            checks.append(("hf_token", token_ok, token_detail))
        if self.hf_checker is None:
            checks.append(
                (
                    "hf_checker_missing",
                    False,
                    "no HFAccessChecker injected; the control plane must assign hf_checker",
                )
            )
        else:
            report = await self.hf_checker.preflight(source)
            checks.extend((f"hf_{check_id}", ok, text) for check_id, ok, text in report.checks)
            if not report.checks:
                checks.append(("hf_preflight", report.ok, "preflight returned no checks"))
        profile = select_profile(spec.family)
        checks.append(("engine_profile", True, f"vLLM profile {profile.name!r}"))
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=tuple(checks))

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        product_id = self._param(spec, "product_id")
        if not product_id:
            raise PlanInputMissing(
                "product_id (the Serverless GPU product) is required",
                detail={"missing": ["product_id"]},
            )
        params = spec.deployment.params
        minimum = int(params.get("min_replicas", 1))
        maximum = int(params.get("max_replicas", minimum))
        gpu_count = int(params.get("gpu_count", 1))
        price = params.get("price_per_gpu_hour_usd")
        return DeploymentPlan(
            gpu_type=str(params.get("gpu_type", product_id)),
            gpu_count=gpu_count,
            replicas_min=minimum,
            replicas_max=maximum,
            max_concurrency=int(params.get("max_concurrency", DEFAULT_MAX_CONCURRENT)),
            est_vram_gb=params.get("est_vram_gb"),
            est_cost_per_hour_usd=None if price is None else float(price) * gpu_count * minimum,
        )

    # -- deploy ------------------------------------------------------------------------------

    def _create_body(
        self, spec: ModelSpec, plan: DeploymentPlan, served_name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The POST body and a copy of it with secret values masked, for the record."""
        source = spec.source
        if not isinstance(source, HFSource):
            raise CapabilityNotSupported("vLLM deployment needs a Hugging Face source")
        port = int(self.config.get("port", DEFAULT_PORT))
        args = build_engine_args(
            spec,
            served_model_name=served_name,
            tensor_parallel_size=plan.gpu_count or 1,
            port=port,
            engine=self._engine(spec),
        )
        envs: list[dict[str, str]] = []
        if source.token_ref:
            token = self._resolve(source.token_ref)
            self._hidden.append(token)
            envs.append({"key": HF_TOKEN_ENV, "value": token})
        body: dict[str, Any] = {
            "type": "sync",
            "name": f"eval-{spec.slug}-{spec.spec_version}",
            "product_id": str(self._param(spec, "product_id")),
            "image": str(self.config["image"]),
            "command": shlex.join(args),
            "envs": envs,
            "worker_config": {
                "min_replicas": plan.replicas_min,
                "max_replicas": plan.replicas_max,
                "max_concurrent_per_worker": plan.max_concurrency or DEFAULT_MAX_CONCURRENT,
                "gpu_num": plan.gpu_count or 1,
                "idle_timeout": int(self.config.get("idle_timeout_s", DEFAULT_IDLE_TIMEOUT_S)),
                # The v2 reference nests request_timeout in worker_config, not at the top level.
                "request_timeout": int(
                    self.config.get("request_timeout_s", DEFAULT_REQUEST_TIMEOUT_S)
                ),
            },
            # Documented as optional, but the live API answers 400 "invalid worker policy"
            # without it: scale on queue wait time (seconds) unless configured otherwise.
            "policy": self._policy(spec),
            "health_check": {
                "path": str(self.config.get("health_path", DEFAULT_HEALTH_PATH)),
                "port": port,
            },
            "ports": [port],
        }
        # Optional v2 worker fields: a disk large enough for weights downloaded at start,
        # and the CUDA floor newer GPUs need (Blackwell cards such as the RTX 5090: 12.8).
        rootfs = self._param(spec, "rootfs_size_gb")
        if rootfs is not None:
            body["worker_config"]["rootfs_size_gb"] = int(rootfs)
        cuda = self._param(spec, "min_cuda_version")
        if cuda is not None:
            body["worker_config"]["min_cuda_version"] = str(cuda)
        if self.config.get("entrypoint"):
            body["entrypoint"] = str(self.config["entrypoint"])
        recorded = dict(body)
        recorded["envs"] = [{"key": env["key"], "value": "***"} for env in envs]
        return body, recorded

    async def _refresh(self, handle: EndpointHandle) -> EndpointState:
        payload = await self._client(handle.credential_ref).get_endpoint(
            handle.external_ids["novita_endpoint_id"]
        )
        url = payload.get("url")
        if isinstance(url, str) and url:
            handle.base_url = url.rstrip("/") + "/v1"
        state, unknown = map_status(payload)
        if unknown is not None:
            handle.observed["unrecognized_status"] = unknown
        handle.observed["novita_status"] = payload.get("status")
        return state

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        served_name = spec.slug
        body, recorded = self._create_body(spec, plan, served_name)
        credential_ref = self._credential_ref(spec)
        remote_id = await self._client(credential_ref).create_endpoint(body)
        source = spec.source
        assert isinstance(source, HFSource)
        handle = EndpointHandle(
            endpoint_id=_endpoint_id(spec),
            model=spec,
            state=EndpointState.DEPLOYING,
            base_url=None,
            upstream_model=served_name,
            protocol="chat_completions",
            credential_ref=credential_ref,
            external_ids={"novita_endpoint_id": remote_id},
        )
        handle.observed.update(
            {
                "deploy_request": recorded,
                "image": body["image"],
                "model_ref": source.mirror_repo or source.repo,
                "revision": source.revision,
                "engine_args": body["command"],
                "min_replicas": plan.replicas_min,
                "max_replicas": plan.replicas_max,
            }
        )
        # One look right after creation: an endpoint that already serves is READY, anything
        # else stays DEPLOYING and the endpoint manager polls ``status``.
        handle.state = await self._refresh(handle)
        return handle

    # -- status / wake -------------------------------------------------------------------------

    async def status(self, handle: EndpointHandle) -> EndpointState:
        if handle.state in (EndpointState.RETIRED, EndpointState.REJECTED):
            return handle.state
        try:
            return await self._refresh(handle)
        except DeploymentNotFound:
            return EndpointState.FAILED  # deleted outside this platform

    async def _probe(self, handle: EndpointHandle, timeout_s: float) -> ProbeResult:
        assert handle.base_url is not None and handle.upstream_model is not None
        return await probe(
            api_base=handle.base_url,
            api_key=self._resolve(handle.credential_ref),
            model=handle.upstream_model,
            clock=self._clock,
            timeout_s=timeout_s,
            not_ready_markers=self._markers(),
            transport=self._transport,
        )

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        if handle.base_url is None:
            await self._refresh(handle)
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no url yet")
        result, elapsed = await wake_loop(
            lambda timeout: self._probe(handle, timeout),
            clock=self._clock,
            timeout_s=timeout_s,
            poll_s=float(self.config.get("wake_poll_interval_s", DEFAULT_WAKE_POLL_S)),
            probe_cap_s=float(self.config.get("status_timeout_s", DEFAULT_PROBE_TIMEOUT_S)),
        )
        if result.outcome == "ok":
            handle.observed["last_wake_s"] = elapsed
            handle.state = EndpointState.WARM
        elif result.outcome in ("not_ready", "timeout"):
            handle.state = EndpointState.SLEEPING
        else:
            raise_for_probe(result, "wake")
        return handle.state

    # -- scale / teardown ----------------------------------------------------------------------

    async def _set_workers(
        self, handle: EndpointHandle, min_replicas: int, max_replicas: int
    ) -> None:
        client = self._client(handle.credential_ref)
        remote_id = handle.external_ids["novita_endpoint_id"]
        current = (await client.get_endpoint(remote_id)).get("worker_config")
        merged: dict[str, Any] = (
            {key: current[key] for key in _WORKER_KEYS if key in current}
            if isinstance(current, Mapping)
            else {}
        )
        merged["min_replicas"] = min_replicas
        merged["max_replicas"] = max_replicas
        await client.update_endpoint(remote_id, {"worker_config": merged})
        handle.observed["min_replicas"] = min_replicas
        handle.observed["max_replicas"] = max_replicas

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        await self._set_workers(handle, min_replicas, max_replicas)
        if max_replicas == 0:
            handle.state = EndpointState.SLEEPING

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        if mode == "sleep":
            maximum = int(handle.observed.get("max_replicas", 1))
            await self._set_workers(handle, 0, maximum)
            handle.state = EndpointState.SLEEPING
            return
        with contextlib.suppress(DeploymentNotFound):  # already gone: terminate is idempotent
            await self._client(handle.credential_ref).delete_endpoint(
                handle.external_ids["novita_endpoint_id"]
            )
        handle.state = EndpointState.RETIRED

    # -- upstream ------------------------------------------------------------------------------

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        if handle.base_url is None or handle.upstream_model is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no url yet")
        return UpstreamRoute(
            api_base=handle.base_url,
            model=handle.upstream_model,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            cold_start_window_s=int(self.config.get("cold_start_window_s", 0)),
        )
