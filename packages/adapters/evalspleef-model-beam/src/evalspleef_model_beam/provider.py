"""``beam-pod``: an OpenAI-compatible SGLang server deployed as a Beam Pod, api mode.

``deploy`` runs the launcher (the Beam SDK in a child process, see ``launcher.py``) to deploy a
Pod with a stable, token-protected URL, then scales it to the planned container count through the
gateway REST API. ``status`` reads the deployment and its containers from the same API and asks
the engine for ``/models`` once a container runs; ``scale`` sets the container count, ``teardown``
scales to zero (sleep) or stops and deletes the deployment (terminate). The image is
digest-pinned and the model revision is a commit sha, so engine, weights and flags are recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from evalspleef.spi.errors import CapabilityNotSupported, EndpointUnavailable
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DEPLOYMENT_ID_KEY,
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    EngineLog,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.secrets import SecretResolver

from . import launcher
from .clock import Clock, SystemClock
from .errors import (
    CredentialRejected,
    DeploymentNotFound,
    LaunchFailed,
    ParallelismMismatch,
    PlanInputMissing,
    StubIdMissing,
)
from .gateway_api import DEFAULT_API_BASE, DEFAULT_REST_BASE, BeamGatewayClient
from .inference import (
    DEFAULT_NOT_READY_MARKERS,
    ProbeResult,
    probe,
    raise_for_probe,
    serves_models,
    wake_loop,
)
from .logs import read_stub_log
from .parallel import Layout, layout

LaunchRunner = Callable[
    [Sequence[str], bytes, Mapping[str, str], str, float], Awaitable[tuple[int, bytes, bytes]]
]
"""``(argv, stdin, env, cwd, timeout_s) -> (returncode, stdout, stderr)``."""

DEFAULT_SDK_REQUIREMENT = "beam-client==0.2.239"
DEFAULT_LAUNCH_TIMEOUT_S = 900.0
DEFAULT_PORT = 8000
DEFAULT_CPU = 8
DEFAULT_MEMORY = "32Gi"
DEFAULT_KEEP_WARM_S = 1800
DEFAULT_MAX_CONCURRENT = 8
DEFAULT_WAKE_POLL_S = 15.0
DEFAULT_PROBE_TIMEOUT_S = 90.0
DEFAULT_STATUS_TIMEOUT_S = 10.0
DEFAULT_MAX_RESTARTS = 2
HF_TOKEN_ENV = "HF_TOKEN"
DEFAULT_FETCH_CPU = 2
DEFAULT_FETCH_MEMORY = "8Gi"
DEFAULT_FETCH_TIMEOUT_S = 3600
DEFAULT_FETCH_POLL_S = 10.0
_TASK_FAILED = frozenset({"ERROR", "CANCELLED", "TIMEOUT"})
_REPO_PATH = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")

# Runs inside the weights Pod (the engine image ships huggingface_hub). snapshot_download into a
# local directory skips files whose recorded commit already matches, so a cached revision costs
# only metadata checks while any other content is replaced by the pinned revision. Inputs come in
# through the environment, never through the command text.
FETCH_SCRIPT = """
import os, time
from huggingface_hub import snapshot_download, __version__
started = time.time()
token_env = os.environ.get("SPLEEF_TOKEN_ENV") or "HF_TOKEN"
path = snapshot_download(
    repo_id=os.environ["SPLEEF_REPO"],
    revision=os.environ["SPLEEF_REVISION"],
    local_dir=os.environ["SPLEEF_DIR"],
    token=os.environ.get(token_env) or None,
)
print("SPLEEF_FETCH_DONE", __version__, round(time.time() - started, 1), path, flush=True)
"""
_PASSED_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "SSL_CERT_FILE", "UV_CACHE_DIR")
_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_STDERR_TAIL = 400


def _endpoint_id(spec: ModelSpec) -> str:
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


def deployment_name(spec: ModelSpec) -> str:
    """A Beam app name: lowercase letters, digits and hyphens, unique per spec version."""
    slug = re.sub(r"[^a-z0-9-]+", "-", spec.slug.lower()).strip("-")
    return f"spleef-{slug}-v{spec.spec_version}"[:63].rstrip("-")


async def run_subprocess(
    argv: Sequence[str], stdin: bytes, env: Mapping[str, str], cwd: str, timeout_s: float
) -> tuple[int, bytes, bytes]:
    """Run the launcher with an explicit deadline; a launcher past it is killed."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise LaunchFailed(
            f"the Beam launcher did not finish within {timeout_s:.0f}s",
            detail={"reason": "launch_timeout"},
        ) from None
    return process.returncode or 0, stdout, stderr


def parse_launch_result(stdout: bytes) -> dict[str, Any] | None:
    """The last ``RESULT_PREFIX`` line of the launcher's stdout, or None when there is none."""
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if line.startswith(launcher.RESULT_PREFIX):
            try:
                parsed = json.loads(line[len(launcher.RESULT_PREFIX) :])
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


class BeamPodProvider(ModelEndpointProvider):
    """Api-mode provider for an SGLang server deployed as a Beam Pod."""

    name = "beam-pod"
    management_mode = ManagementMode.API
    hf_checker: HFAccessChecker | None = None
    """Optional extra Hugging Face preflight inside ``validate``.

    The endpoint manager resolves the revision and runs the preflight with its own checker
    before it calls ``validate`` and assigns nothing here, so an unset checker is not a failure.
    """

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
        launch_runner: LaunchRunner | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._launch: LaunchRunner = launch_runner if launch_runner is not None else run_subprocess
        self._api_transport = api_transport
        self._transport = transport
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
        return f"env://BEAM_TOKEN_{suffix}"

    def _resolve(self, ref: str) -> str:
        try:
            value = self.secrets.resolve(ref)
        except (KeyError, ValueError) as exc:
            raise CredentialRejected(
                f"the secret reference {ref} does not resolve",
                detail={"reason": "secret_unresolved"},
            ) from exc
        self._hidden.append(value)
        return value

    def _client(self, credential_ref: str) -> BeamGatewayClient:
        return BeamGatewayClient(
            lambda: self._resolve(credential_ref),
            api_base=str(self.config.get("api_base", DEFAULT_API_BASE)),
            rest_base=str(self.config.get("rest_base", DEFAULT_REST_BASE)),
            transport=self._api_transport,
            hidden=lambda: tuple(self._hidden),
        )

    def _param(self, spec: ModelSpec, key: str, default: Any = None) -> Any:
        value = spec.deployment.params.get(key)
        return value if value is not None else self.config.get(key, default)

    def _launcher_argv(self) -> list[str]:
        raw = self.config.get("launcher_argv")
        if isinstance(raw, (list, tuple)) and raw:
            return [str(arg) for arg in raw]
        requirement = str(self.config.get("sdk_requirement", DEFAULT_SDK_REQUIREMENT))
        return [
            "uv",
            "run",
            "--no-project",
            "--quiet",
            "--python",
            "3.12",
            "--with",
            requirement,
            "python",
            "-I",
            str(Path(launcher.__file__).resolve()),
        ]

    def _markers(self) -> tuple[str, ...]:
        raw = self.config.get("not_ready_markers")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(marker).lower() for marker in raw)
        return DEFAULT_NOT_READY_MARKERS

    def _engine(self, spec: ModelSpec) -> str:
        engine = str(self._param(spec, "engine", "sglang"))
        if engine != "sglang":
            raise CapabilityNotSupported(
                f"Beam engine {engine!r} is not supported (sglang)", detail={"engine": engine}
            )
        return engine

    # -- validate / plan -------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        checks: list[tuple[str, bool, str]] = []
        credential_ref = self._credential_ref(spec)
        try:
            resolved = bool(self.secrets.resolve(credential_ref))
            detail = credential_ref if resolved else f"{credential_ref} resolves to an empty value"
        except (KeyError, ValueError) as exc:
            resolved, detail = False, f"{credential_ref} does not resolve ({type(exc).__name__})"
        checks.append(("beam_token", resolved, detail))
        image = str(self.config.get("image", ""))
        checks.append(
            (
                "image_digest_pinned",
                bool(_DIGEST_PINNED.search(image)),
                f"image {image!r} must end in @sha256:<64 hex>",
            )
        )
        checks.append(("gpu", bool(self._param(spec, "gpu")), "Beam GPU type, e.g. RTX5090"))
        program = self._launcher_argv()[0]
        checks.append(
            ("launcher", shutil.which(program) is not None, f"{program!r} must be on PATH")
        )
        try:
            engine = self._engine(spec)
            checks.append(("engine", True, engine))
        except CapabilityNotSupported as exc:
            checks.append(("engine", False, str(exc)))
        gpu_count = int(spec.deployment.params.get("gpu_count", 1))
        if gpu_count > 1 and resolved:
            checks.append(await self._multi_gpu_check(credential_ref, gpu_count))
        try:
            parallel = self._layout(spec)
            checks.append(
                (
                    "parallelism",
                    True,
                    f"tp={parallel.tp} pp={parallel.pp} dp={parallel.dp} ep={parallel.ep}",
                )
            )
        except ParallelismMismatch as exc:
            checks.append(("parallelism", False, str(exc)))
        source = spec.source
        if not isinstance(source, HFSource):
            checks.append(("model_source", False, "a Beam Pod deployment needs an HF source"))
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
        weights = self._weights(spec)
        if weights is not None:
            repo = source.mirror_repo or source.repo
            safe = bool(_REPO_PATH.match(repo)) and ".." not in repo.split("/")
            checks.append(("weights_path", safe, f"{repo!r} becomes the directory {weights[2]}"))
        if self.hf_checker is not None:
            report = await self.hf_checker.preflight(source)
            checks.extend((f"hf_{check_id}", ok, text) for check_id, ok, text in report.checks)
            if not report.checks:
                checks.append(("hf_preflight", report.ok, "preflight returned no checks"))
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=tuple(checks))

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        gpu = self._param(spec, "gpu")
        if not gpu:
            raise PlanInputMissing(
                "gpu (the Beam GPU type, e.g. RTX5090) is required", detail={"missing": ["gpu"]}
            )
        params = spec.deployment.params
        minimum = int(params.get("min_replicas", 1))
        maximum = int(params.get("max_replicas", minimum))
        gpu_count = int(params.get("gpu_count", 1))
        self._layout(spec)
        price = params.get("price_per_gpu_hour_usd")
        return DeploymentPlan(
            gpu_type=str(gpu),
            gpu_count=gpu_count,
            replicas_min=minimum,
            replicas_max=maximum,
            max_concurrency=int(params.get("max_concurrency", DEFAULT_MAX_CONCURRENT)),
            est_vram_gb=params.get("est_vram_gb"),
            est_cost_per_hour_usd=None if price is None else float(price) * gpu_count * minimum,
        )

    # -- deploy ------------------------------------------------------------------------------

    async def _multi_gpu_check(self, credential_ref: str, gpu_count: int) -> tuple[str, bool, str]:
        """Whether the workspace may run ``gpu_count`` GPUs in one container."""
        client = self._client(credential_ref)
        workspace = await client.current_workspace()
        if not workspace.get("multi_gpu_enabled"):
            return (
                "multi_gpu",
                False,
                "multi-GPU containers are not enabled for this Beam workspace",
            )
        limits = await client.workspace_limits(str(workspace.get("external_id") or ""))
        ceiling = limits.get("max_gpu_count")
        if isinstance(ceiling, int) and gpu_count > ceiling:
            return ("multi_gpu", False, f"the workspace allows {ceiling} GPUs per container")
        return ("multi_gpu", True, f"{gpu_count} GPUs allowed")

    def _layout(self, spec: ModelSpec) -> Layout:
        extra = [str(arg) for arg in self._param(spec, "engine_extra_args") or []]
        return layout(int(spec.deployment.params.get("gpu_count", 1)), extra)

    def _weights(self, spec: ModelSpec) -> tuple[str, str, str] | None:
        """``(volume, mount path, weights directory)`` when weights come from a Beam Volume."""
        volume = self._param(spec, "weights_volume")
        if not volume:
            return None
        source = spec.source
        assert isinstance(source, HFSource)
        mount = str(self._param(spec, "weights_mount", f"/{volume}")).rstrip("/")
        return str(volume), mount, f"{mount}/{source.mirror_repo or source.repo}"

    def _engine_args(self, spec: ModelSpec, plan: DeploymentPlan, served_name: str) -> list[str]:
        source = spec.source
        assert isinstance(source, HFSource)
        self._engine(spec)
        context = int(self._param(spec, "context_length", spec.declared_context_tokens))
        extra = self._param(spec, "engine_extra_args") or []
        weights = self._weights(spec)
        # From a Volume the engine loads the directory the weights step pinned to the revision.
        model = (
            ["--model-path", weights[2]]
            if weights is not None
            else ["--model-path", source.mirror_repo or source.repo, "--revision", source.revision]
        )
        return [
            "python3",
            "-m",
            "sglang.launch_server",
            *model,
            "--served-model-name",
            served_name,
            "--host",
            "0.0.0.0",
            "--port",
            str(int(self.config.get("port", DEFAULT_PORT))),
            "--context-length",
            str(context),
            *([] if self._layout(spec).explicit else ["--tp-size", str(plan.gpu_count or 1)]),
            "--max-running-requests",
            str(plan.max_concurrency or DEFAULT_MAX_CONCURRENT),
            *[str(arg) for arg in extra],
        ]

    def _launch_request(
        self, spec: ModelSpec, plan: DeploymentPlan, served_name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The launcher request and a copy of it with secret values masked, for the record."""
        source = spec.source
        if not isinstance(source, HFSource):
            raise CapabilityNotSupported("a Beam Pod deployment needs a Hugging Face source")
        env: dict[str, str] = {}
        weights = self._weights(spec)
        if source.token_ref and weights is None:
            env[HF_TOKEN_ENV] = self._resolve(source.token_ref)
        request: dict[str, Any] = {
            "action": "deploy",
            "name": deployment_name(spec),
            "image": str(self.config["image"]),
            "entrypoint": self._engine_args(spec, plan, served_name),
            "ports": [int(self.config.get("port", DEFAULT_PORT))],
            "cpu": self._param(spec, "cpu", DEFAULT_CPU),
            "memory": str(self._param(spec, "memory", DEFAULT_MEMORY)),
            "gpu": str(plan.gpu_type),
            "gpu_count": plan.gpu_count or 1,
            "env": env,
            "secrets": [str(name) for name in self._param(spec, "beam_secrets", None) or []],
            "keep_warm_seconds": int(self._param(spec, "keep_warm_s", DEFAULT_KEEP_WARM_S)),
            "volumes": [] if weights is None else [{"name": weights[0], "mount_path": weights[1]}],
        }
        recorded = dict(request)
        recorded["env"] = {key: "***" for key in env}
        return request, recorded

    def _fetch_request(
        self, spec: ModelSpec, weights: tuple[str, str, str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """A CPU-only Pod run that puts the pinned revision into the Volume."""
        source = spec.source
        assert isinstance(source, HFSource)
        volume, mount, directory = weights
        token_secret = self._param(spec, "hf_token_secret")
        env = {
            "SPLEEF_REPO": source.repo,
            "SPLEEF_REVISION": source.revision,
            "SPLEEF_DIR": directory,
            "SPLEEF_TOKEN_ENV": str(token_secret or HF_TOKEN_ENV),
        }
        secrets = [str(name) for name in self._param(spec, "beam_secrets", None) or []]
        if token_secret:
            secrets.append(str(token_secret))  # the token stays in Beam's secret store
        elif source.token_ref:
            env[HF_TOKEN_ENV] = self._resolve(source.token_ref)
        timeout_s = int(self._param(spec, "fetch_timeout_s", DEFAULT_FETCH_TIMEOUT_S))
        request: dict[str, Any] = {
            "action": "fetch",
            "name": f"{deployment_name(spec)}-weights"[:63],
            "image": str(self.config.get("fetch_image") or self.config["image"]),
            "entrypoint": ["python3", "-c", FETCH_SCRIPT],
            "ports": [],
            "cpu": self._param(spec, "fetch_cpu", DEFAULT_FETCH_CPU),
            "memory": str(self._param(spec, "fetch_memory", DEFAULT_FETCH_MEMORY)),
            "gpu": "",
            "gpu_count": 0,
            "env": env,
            "secrets": secrets,
            "keep_warm_seconds": timeout_s,
            "volumes": [{"name": volume, "mount_path": mount}],
        }
        recorded = dict(request)
        recorded["env"] = {
            key: "***" if key == HF_TOKEN_ENV else value for key, value in env.items()
        }
        return request, recorded

    async def _fetch_weights(
        self, spec: ModelSpec, credential_ref: str, weights: tuple[str, str, str]
    ) -> dict[str, Any]:
        """Run the weights Pod to completion; any other end raises ``LaunchFailed``."""
        request, recorded = self._fetch_request(spec, weights)
        result = await self._run_launcher(credential_ref, request, required="task_id")
        client = self._client(credential_ref)
        workspace_id = await client.current_workspace_id()
        started = self._clock.now()
        deadline = started + float(request["keep_warm_seconds"])
        poll_s = float(self.config.get("fetch_poll_s", DEFAULT_FETCH_POLL_S))
        record = {
            "fetch_request": recorded,
            "fetch_task_id": result["task_id"],
            "fetch_container_id": result.get("container_id", ""),
        }
        while True:
            task = await client.get_task(workspace_id, str(result["task_id"]))
            status = str(task.get("status") or "").upper()
            if status == "COMPLETE":
                return {**record, "fetch_status": status, "fetch_s": self._clock.now() - started}
            if status in _TASK_FAILED:
                raise LaunchFailed(
                    f"the weights download for {request['name']} ended {status}",
                    detail={
                        "reason": "fetch_failed",
                        "task_status": status,
                        "failure_reason": self._scrub(str(task.get("failure_reason") or "")),
                        **record,
                    },
                )
            if self._clock.now() + poll_s > deadline:
                container_id = str(result.get("container_id") or "")
                if container_id:
                    with contextlib.suppress(DeploymentNotFound):
                        await client.stop_container(container_id)
                raise LaunchFailed(
                    f"the weights download for {request['name']} did not finish in time",
                    detail={"reason": "fetch_timeout", "task_status": status, **record},
                )
            await self._clock.sleep(poll_s)

    def _scrub(self, text: str) -> str:
        for secret in self._hidden:
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _remove_by_name(self, credential_ref: str, name: str) -> list[str]:
        """Stop and delete every live deployment named ``name``; a failed launch may leave one."""
        client = self._client(credential_ref)
        removed: list[str] = []
        for record in await client.list_deployments():
            deployment_id = str(record.get("id") or "")
            if record.get("name") != name or not deployment_id:
                continue
            with contextlib.suppress(DeploymentNotFound):
                await client.stop_deployment(deployment_id)
            with contextlib.suppress(DeploymentNotFound):
                await client.delete_deployment(deployment_id)
            removed.append(deployment_id)
        return removed

    async def _run_launcher(
        self, credential_ref: str, request: Mapping[str, Any], *, required: str = "deployment_id"
    ) -> dict[str, Any]:
        token = self._resolve(credential_ref)
        timeout_s = float(self.config.get("launch_timeout_s", DEFAULT_LAUNCH_TIMEOUT_S))
        with tempfile.TemporaryDirectory(prefix="spleef-beam-") as scratch:
            env = {key: os.environ[key] for key in _PASSED_ENV if key in os.environ}
            env["BEAM_TOKEN"] = token
            env["CONFIG_PATH"] = str(Path(scratch) / "config.ini")
            try:
                code, stdout, stderr = await self._launch(
                    self._launcher_argv(), json.dumps(request).encode(), env, scratch, timeout_s
                )
            except LaunchFailed as exc:
                removed = await self._remove_by_name(credential_ref, str(request["name"]))
                raise LaunchFailed(str(exc), detail={**exc.detail, "removed": removed}) from exc
        result = parse_launch_result(stdout)
        if result is None or not result.get("ok") or not result.get(required):
            removed = await self._remove_by_name(credential_ref, str(request["name"]))
            reason = (result or {}).get("error") or f"launcher exited {code} without a result"
            tail = self._scrub(stderr.decode(errors="replace"))[-_STDERR_TAIL:]
            # The SDK prints its own failure (quota, GPU type, image build) to stdout.
            said = [
                line.strip()
                for line in stdout.decode(errors="replace").splitlines()
                if line.strip() and not line.startswith(launcher.RESULT_PREFIX)
            ]
            sdk_tail = self._scrub("\n".join(said))[-_STDERR_TAIL:]
            # The SDK writes its failure line (quota, GPU type, image build) to stderr and its
            # progress to stdout; the last stderr line is the reason when there is one.
            complaints = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
            complaints = [line for line in complaints if line]
            last = complaints[-1] if complaints else (said[-1] if said else "")
            if last:
                reason = f"{reason}; the SDK said: {self._scrub(last)}"
            raise LaunchFailed(
                self._scrub(f"Beam deployment of {request['name']} failed: {reason}"),
                detail={
                    "reason": "launch_failed",
                    "stderr_tail": tail,
                    "sdk_output_tail": sdk_tail,
                    "removed": removed,
                },
            )
        return result

    async def _refresh(self, handle: EndpointHandle) -> EndpointState:
        client = self._client(handle.credential_ref)
        deployment_id = handle.external_ids["beam_deployment_id"]
        record = next(
            (item for item in await client.list_deployments() if item.get("id") == deployment_id),
            None,
        )
        if record is None or record.get("active") is False:
            handle.observed["beam_deployment_active"] = False
            return EndpointState.FAILED  # stopped or deleted outside this platform
        containers = [
            item
            for item in await client.list_containers()
            if item.get("deploymentId") == deployment_id
        ]
        statuses = sorted(str(item.get("status") or "").upper() for item in containers)
        handle.observed["beam_containers"] = statuses
        # Beam replaces a container whose engine exits, under a new id. Every id seen since the
        # engine last served is one start; past the limit the deployment is crash-looping and is
        # reported FAILED instead of billing as DEPLOYING forever.
        started = set(handle.observed.get("beam_starts_unserved", ()))
        started |= {str(item.get("containerId")) for item in containers if item.get("containerId")}
        serving = False
        if "RUNNING" in statuses:
            assert handle.base_url is not None
            serving = await serves_models(
                api_base=handle.base_url,
                api_key=self._resolve(handle.credential_ref),
                timeout_s=float(self.config.get("status_timeout_s", DEFAULT_STATUS_TIMEOUT_S)),
                transport=self._transport,
            )
        if serving:
            handle.observed["beam_starts_unserved"] = []
            return EndpointState.READY
        handle.observed["beam_starts_unserved"] = sorted(started)
        restarts = len(started) - max(1, int(handle.observed.get("containers", 1)))
        limit = int(self.config.get("max_container_restarts", DEFAULT_MAX_RESTARTS))
        if restarts > limit:
            handle.observed["failure"] = (
                f"crash loop: {len(started)} containers started without the engine serving"
            )
            return EndpointState.FAILED
        if statuses:
            return EndpointState.DEPLOYING
        if int(handle.observed.get("containers", 1)) == 0:
            return EndpointState.SLEEPING
        return EndpointState.DEPLOYING

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        served_name = spec.slug
        request, recorded = self._launch_request(spec, plan, served_name)
        credential_ref = self._credential_ref(spec)
        weights = self._weights(spec)
        fetched: dict[str, Any] = {}
        if weights is not None:
            fetched = await self._fetch_weights(spec, credential_ref, weights)
            fetched.update({"weights_volume": weights[0], "weights_dir": weights[2]})
        result = await self._run_launcher(credential_ref, request)
        invoke_url = str(result.get("invoke_url") or "").rstrip("/")
        if not invoke_url:
            raise LaunchFailed(
                f"Beam deployed {request['name']} without an invoke url",
                detail={"reason": "malformed", "deployment_id": result["deployment_id"]},
            )
        source = spec.source
        assert isinstance(source, HFSource)
        handle = EndpointHandle(
            endpoint_id=_endpoint_id(spec),
            model=spec,
            state=EndpointState.DEPLOYING,
            base_url=invoke_url + "/v1",
            upstream_model=served_name,
            protocol="chat_completions",
            credential_ref=credential_ref,
            external_ids={
                "beam_deployment_id": str(result["deployment_id"]),
                DEPLOYMENT_ID_KEY: str(result["deployment_id"]),
                "beam_stub_id": str(result.get("stub_id") or ""),
            },
        )
        handle.observed.update(
            {
                "deploy_request": recorded,
                "image": request["image"],
                "model_ref": source.mirror_repo or source.repo,
                "revision": source.revision,
                "engine_args": request["entrypoint"],
                "beam_deployment_name": request["name"],
                "beam_version": result.get("version"),
                "sdk_requirement": str(self.config.get("sdk_requirement", DEFAULT_SDK_REQUIREMENT)),
                "min_replicas": plan.replicas_min,
                "max_replicas": plan.replicas_max,
                **fetched,
            }
        )
        await self._set_containers(handle, plan.replicas_min)
        # One look right after creation: a deployment that already serves is READY, anything
        # else stays DEPLOYING and the endpoint manager polls ``status``.
        handle.state = await self._refresh(handle)
        return handle

    # -- status / wake -------------------------------------------------------------------------

    async def status(self, handle: EndpointHandle) -> EndpointState:
        if handle.state in (EndpointState.RETIRED, EndpointState.REJECTED):
            return handle.state
        return await self._refresh(handle)

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
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no url")
        if int(handle.observed.get("containers", 1)) == 0:
            await self._set_containers(handle, max(1, int(handle.observed.get("max_replicas", 1))))
        result, elapsed = await wake_loop(
            lambda timeout: self._probe(handle, timeout),
            clock=self._clock,
            timeout_s=timeout_s,
            poll_s=float(self.config.get("wake_poll_interval_s", DEFAULT_WAKE_POLL_S)),
            probe_cap_s=float(self.config.get("probe_timeout_s", DEFAULT_PROBE_TIMEOUT_S)),
        )
        if result.outcome == "ok":
            handle.observed["last_wake_s"] = elapsed
            handle.state = EndpointState.WARM
        elif result.outcome in ("not_ready", "timeout"):
            handle.state = EndpointState.DEPLOYING
        else:
            raise_for_probe(result, "wake")
        return handle.state

    # -- scale / teardown ----------------------------------------------------------------------

    async def _set_containers(self, handle: EndpointHandle, containers: int) -> None:
        await self._client(handle.credential_ref).scale_deployment(
            handle.external_ids["beam_deployment_id"], containers
        )
        handle.observed["containers"] = containers

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        """Run ``max_replicas`` containers.

        A Beam Pod deployment holds a fixed container count rather than autoscaling between
        bounds, so the upper bound is provisioned: it satisfies both bounds.
        """
        await self._set_containers(handle, max_replicas)
        handle.observed["min_replicas"] = min_replicas
        handle.observed["max_replicas"] = max_replicas
        if max_replicas == 0:
            handle.state = EndpointState.SLEEPING

    async def engine_log(self, handle: EndpointHandle, *, max_bytes: int) -> EngineLog:
        """The Pod's container log (newest ``max_bytes``), scrubbed of resolved secrets.

        The log API filters on the stub id only; without one the request would return the
        workspace's other deployments, so no request is sent.
        """
        stub_id = handle.external_ids.get("beam_stub_id", "")
        if not stub_id:
            raise StubIdMissing(
                f"endpoint {handle.endpoint_id} has no Beam stub id; refusing an unfiltered log read",
                detail={"reason": "stub_id_missing"},
            )
        client = self._client(handle.credential_ref)
        workspace_id = await client.current_workspace_id()

        async def page(query: Mapping[str, str | int]) -> list[dict[str, Any]]:
            return await client.get_log_page(workspace_id, query)

        fetched = await read_stub_log(page, stub_id, max_bytes)
        data = self._scrub("\n".join(fetched.lines)).encode()
        cut = len(data) > max_bytes
        text = data[len(data) - max_bytes :] if cut else data
        return EngineLog(
            text.decode(errors="ignore"),
            truncated=fetched.truncated or cut,
            dropped=fetched.dropped,
        )

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        if mode == "sleep":
            await self._set_containers(handle, 0)
            handle.state = EndpointState.SLEEPING
            return
        client = self._client(handle.credential_ref)
        deployment_id = handle.external_ids["beam_deployment_id"]
        with contextlib.suppress(DeploymentNotFound):  # already gone: terminate is idempotent
            await client.stop_deployment(deployment_id)
        with contextlib.suppress(DeploymentNotFound):
            await client.delete_deployment(deployment_id)
        handle.observed["containers"] = 0
        handle.state = EndpointState.RETIRED

    # -- upstream ------------------------------------------------------------------------------

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        if handle.base_url is None or handle.upstream_model is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no url")
        # Request defaults the gateway applies (e.g. chat_template_kwargs selecting a model's
        # thinking mode); taken from the spec first so the frozen spec records them.
        defaults = self._param(handle.model, "default_params") or {}
        return UpstreamRoute(
            api_base=handle.base_url,
            model=handle.upstream_model,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            default_params=dict(defaults),
            cold_start_window_s=int(self.config.get("cold_start_window_s", 0)),
        )
