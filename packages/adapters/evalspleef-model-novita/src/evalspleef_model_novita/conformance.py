"""Conformance targets for the Novita providers, run against fakes of Novita.

This module is the entry point of ``evalspleef.conformance_targets``. It is the only module of
the adapter that may import the conformance kit and the test doubles.

Both targets set ``fake=True``: nothing here talks to a real Novita account.

* ``novita-dedicated-fake``: a local OpenAI-compatible server that behaves like a
  cold-starting dedicated endpoint on an injected clock. It only stands in for the console
  deployment step an operator performs by hand.
* ``novita-serverless-fake``: an ``httpx.MockTransport`` implementing the Serverless GPUs v2
  REST API in memory, in front of a local OpenAI-compatible server for the inference plane.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from evalspleef.conformance.cases_model import (
    HF_BIN_ONLY_REPO,
    HF_GATED_REPO,
    HF_PRIVATE_REPO,
    HF_PUBLIC_REPO,
)
from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelSpec,
    ValidationReport,
)
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver
from evalspleef_model_novita.dedicated import NovitaDedicatedProvider
from evalspleef_model_novita.serverless_gpu import NovitaServerlessGpuProvider

API_KEY = "sk-novita-conformance"
API_KEY_REF = "vault://novita/conformance"
HF_TOKEN_REF = "vault://hf/conformance"
DEDICATED_UPSTREAM_MODEL = "evalspleef-conformance/public-model"
SERVERLESS_SLUG = "novita-serverless-conf"
IMAGE = "vllm/vllm-openai@sha256:" + "a" * 64
_REVISION = "0" * 40
_WEIGHT_BYTES = 2_000_000_000


class SteppingClock:
    """Deterministic clock whose ``sleep`` advances time instead of waiting for it."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot move backwards")
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))


class FakeHFChecker(HFAccessChecker):
    """HF access double that answers for the four fixture repos of the M-HF case."""

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        return revision if len(revision) == 40 else _REVISION

    async def preflight(self, source: HFSource) -> ValidationReport:
        checks: tuple[tuple[str, bool, str], ...]
        if source.repo == HF_GATED_REPO:
            checks = (("licence_accepted", False, "gated model: the licence is not accepted"),)
        elif source.repo == HF_PRIVATE_REPO:
            checks = (("repo_readable", False, "private repository: no permission for this token"),)
        elif source.repo == HF_BIN_ONLY_REPO:
            checks = (("safetensors", False, "only .bin weights found; safetensors required"),)
        elif source.repo == HF_PUBLIC_REPO:
            checks = (
                ("repo_readable", True, "readable"),
                ("safetensors", True, "model.safetensors present"),
                ("architectures", True, "architectures set"),
                ("weight_bytes", True, str(_WEIGHT_BYTES)),
            )
        else:
            checks = (("repo_readable", False, f"repository {source.repo} not found"),)
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=checks)

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return f"mirror-{source.repo.rsplit('/', 1)[-1]}"

    async def verify_mirror(self, source: HFSource) -> bool:
        return True


_ERROR_BODIES: dict[int, tuple[str, str]] = {
    401: ("invalid_request_error", "invalid_api_key"),
    403: ("invalid_request_error", "deployment_not_owned"),
    404: ("invalid_request_error", "deployment_not_found"),
    422: ("invalid_request_error", "unprocessable_entity"),
    429: ("rate_limit_error", "rate_limit_exceeded"),
    500: ("server_error", "internal_server_error"),
    503: ("server_error", "service_unavailable"),
}


class FakeNovitaInferenceServer(FakeOpenAIServer):
    """A dedicated endpoint that can be asleep and cold-start on an injected clock.

    ``go_to_sleep`` puts it to sleep. The next request starts the cold start: in ``reject``
    mode requests are answered 503 'service not ready' until ``cold_start_s`` has passed on the
    clock; in ``hold`` mode the first request is held, which advances the clock by the whole
    cold start before it is answered, like a slow first answer.
    """

    def __init__(
        self,
        *,
        clock: SteppingClock,
        cold_start_s: float = 300.0,
        hold: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(clock=clock, **kwargs)
        self._stepping = clock
        self.cold_start_s = cold_start_s
        self.hold = hold
        self.not_ready_count = 0
        self._asleep = False
        self._wake_at: float | None = None
        self._forced: deque[int] = deque()

    def go_to_sleep(self) -> None:
        self._asleep = True
        self._wake_at = None

    def inject_status(self, status: int, *, count: int = 1) -> None:
        """Answer the next ``count`` chat calls with HTTP ``status``."""
        if status not in _ERROR_BODIES:
            raise ValueError(f"unsupported injected status: {status}")
        self._forced.extend([status] * count)

    async def _chat(self, request: Request) -> Response:
        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error
        if self._forced:
            status = self._forced.popleft()
            error_type, code = _ERROR_BODIES[status]
            message = f"injected error {status}"
            return JSONResponse(
                {"error": {"message": message, "type": error_type, "code": code}},
                status_code=status,
            )
        if self._asleep:
            now = self._stepping.now()
            if self._wake_at is None:
                if self.hold:
                    self._stepping.advance(self.cold_start_s)
                    self._asleep = False
                else:
                    self._wake_at = now + self.cold_start_s
            if self._asleep:
                assert self._wake_at is not None
                if now < self._wake_at:
                    self.not_ready_count += 1
                    return JSONResponse(
                        {
                            "error": {
                                "message": "model is loading: service not ready",
                                "type": "server_error",
                                "code": "service_not_ready",
                            }
                        },
                        status_code=503,
                    )
                self._asleep = False
        return await super()._chat(request)


class FakeNovitaGpuApi:
    """In-memory Serverless GPUs v2 REST API: create, get, update, delete."""

    def __init__(self, *, api_key: str, inference_url: str) -> None:
        self.api_key = api_key
        self.inference_url = inference_url
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.status_value = "running"
        self._counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _json(status: int, payload: Mapping[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=dict(payload))

    def _error(self, status: int, message: str) -> httpx.Response:
        return self._json(status, {"code": status, "message": message})

    def _worker_view(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        if record["worker_config"].get("min_replicas", 0) <= 0:
            return []
        return [
            {
                "id": "worker-1",
                "status": {"status": "ready", "error": "", "message": ""},
                "health": True,
                "drain": False,
            }
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] | None = None
        if request.content:
            parsed = json.loads(request.content)
            body = parsed if isinstance(parsed, dict) else None
        self.calls.append((request.method, request.url.path, body))
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return self._error(401, "invalid api key")
        path = request.url.path
        if path == "/gpus/v2/endpoints" and request.method == "POST":
            return self._create(body or {})
        prefix = "/gpus/v2/endpoints/"
        if not path.startswith(prefix):
            return self._error(404, "no such route")
        endpoint_id = path[len(prefix) :]
        record = self.endpoints.get(endpoint_id)
        if record is None:
            return self._error(404, f"endpoint {endpoint_id} not found")
        if request.method == "GET":
            view = dict(record)
            view["status"] = {"status": self.status_value, "error": "", "message": ""}
            view["url"] = self.inference_url
            view["workers"] = self._worker_view(record)
            return self._json(200, view)
        if request.method == "PUT":
            update = body or {}
            if "worker_config" in update:
                record["worker_config"] = {**record["worker_config"], **update["worker_config"]}
            return self._json(200, {})
        if request.method == "DELETE":
            del self.endpoints[endpoint_id]
            return self._json(200, {})
        return self._error(405, "method not allowed")

    def _create(self, body: Mapping[str, Any]) -> httpx.Response:
        missing = [
            key
            for key in ("type", "name", "product_id", "image", "worker_config")
            if key not in body
        ]
        worker_config = body.get("worker_config")
        if not missing and "max_replicas" not in (worker_config or {}):
            missing.append("worker_config.max_replicas")
        if missing:
            return self._error(400, f"missing required fields: {', '.join(missing)}")
        self._counter += 1
        endpoint_id = f"fake-endpoint-{self._counter}"
        self.endpoints[endpoint_id] = {"id": endpoint_id, **body}
        return self._json(200, {"id": endpoint_id})


def _dedicated_spec() -> ModelSpec:
    return ModelSpec(
        slug="novita-dedicated-conf",
        spec_version=1,
        family="qwen3",
        roles=frozenset({"target"}),
        source=HFSource(repo=HF_PUBLIC_REPO, revision=_REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="novita-dedicated",
            account="conformance",
            params={
                "model": DEDICATED_UPSTREAM_MODEL,
                "weight_bytes": _WEIGHT_BYTES,
                "num_hidden_layers": 28,
                "num_key_value_heads": 8,
                "head_dim": 128,
                "batch_label": "conformance",
            },
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="novita-dedicated",
        license="apache-2.0",
        owner="evalspleef",
    )


def _serverless_spec() -> ModelSpec:
    return ModelSpec(
        slug=SERVERLESS_SLUG,
        spec_version=1,
        family="qwen3",
        roles=frozenset({"target"}),
        source=HFSource(repo=HF_PUBLIC_REPO, revision=_REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="novita-serverless-gpu",
            account="conformance",
            params={"product_id": "prod-conformance", "min_replicas": 1, "max_replicas": 1},
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="novita-serverless-gpu",
        license="apache-2.0",
        owner="evalspleef",
    )


def _secrets() -> DictSecretResolver:
    return DictSecretResolver({API_KEY_REF: API_KEY, HF_TOKEN_REF: "hf_conformance"})


def _dedicated_target() -> ConformanceTarget:
    clock = SteppingClock()
    server = FakeNovitaInferenceServer(clock=clock, model=DEDICATED_UPSTREAM_MODEL, api_key=API_KEY)
    checker = FakeHFChecker()
    provider = NovitaDedicatedProvider(
        "conformance",
        {"api_key_ref": API_KEY_REF, "allow_loopback_http": True},
        _secrets(),
        clock=clock,
    )
    provider.hf_checker = checker

    def operator_input() -> Mapping[str, str]:
        # The operator's console step is faked by starting the local endpoint on demand.
        server.start()
        return {
            "base_url": server.base_url,
            "model": DEDICATED_UPSTREAM_MODEL,
            "deployment_id": "dep-conformance-1",
        }

    async def upstream_status(status: int, count: int = 1) -> None:
        server.inject_status(status, count=count)

    async def cleanup() -> None:
        server.stop()

    return ConformanceTarget(
        name="novita-dedicated-fake",
        kind="model",
        provider=provider,
        fake=True,
        injectors={"upstream_status": upstream_status},
        model_fixture=ModelFixture(
            spec_factory=_dedicated_spec,
            operator_input_factory=operator_input,
            upstream=server,
            hf_access=checker,
        ),
        notes=(
            "not verified against a real Novita account: the endpoint is a local fake that "
            "cold-starts on an injected clock; http://127.0.0.1 is accepted via allow_loopback_http"
        ),
        cleanup=cleanup,
    )


def _serverless_target() -> ConformanceTarget:
    clock = SteppingClock()
    server = FakeNovitaInferenceServer(clock=clock, model=SERVERLESS_SLUG, api_key=API_KEY)
    api = FakeNovitaGpuApi(api_key=API_KEY, inference_url="")
    checker = FakeHFChecker()

    def handle(request: httpx.Request) -> httpx.Response:
        # The inference server starts on first use; its address is only known then.
        server.start()
        api.inference_url = server.base_url.removesuffix("/v1")
        return api.handle(request)

    provider = NovitaServerlessGpuProvider(
        "conformance",
        {"api_key_ref": API_KEY_REF, "image": IMAGE},
        _secrets(),
        clock=clock,
        api_transport=httpx.MockTransport(handle),
    )
    provider.hf_checker = checker

    async def upstream_status(status: int, count: int = 1) -> None:
        server.inject_status(status, count=count)

    async def cleanup() -> None:
        server.stop()

    return ConformanceTarget(
        name="novita-serverless-fake",
        kind="model",
        provider=provider,
        fake=True,
        injectors={"upstream_status": upstream_status},
        model_fixture=ModelFixture(
            spec_factory=_serverless_spec,
            operator_input_factory=None,
            upstream=server,
            hf_access=checker,
        ),
        notes=(
            "not verified against a real Novita account: the Serverless GPUs v2 REST API is an "
            "in-memory httpx.MockTransport in front of a local OpenAI-compatible server"
        ),
        cleanup=cleanup,
    )


def build_targets() -> list[ConformanceTarget]:
    """The two Novita targets, both running against fakes."""
    return [_dedicated_target(), _serverless_target()]
