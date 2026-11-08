"""Conformance target for the Beam provider, run against fakes of Beam.

This module is the entry point of ``evalspleef.conformance_targets``. It is the only module of
the adapter that may import the conformance kit and the test doubles.

``beam-pod-fake`` sets ``fake=True``: nothing here talks to a real Beam workspace. The launcher
runs in-process with a fake of the Beam SDK module (so ``launcher.deploy`` itself is exercised),
the gateway REST API is an in-memory ``httpx.MockTransport``, and the Pod's URL is a local
OpenAI-compatible server.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
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
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec, ValidationReport
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver
from evalspleef_model_beam import launcher
from evalspleef_model_beam.provider import BeamPodProvider

TOKEN = "beam-conformance-token"
TOKEN_REF = "vault://beam/conformance"
HF_TOKEN_REF = "vault://hf/conformance"
SLUG = "beam-pod-conf"
IMAGE = "lmsysorg/sglang@sha256:" + "b" * 64
GATEWAY_PREFIX = "/api/v1/gateway"
_REVISION = "0" * 40

_ERROR_BODIES: dict[int, tuple[str, str]] = {
    401: ("invalid_request_error", "invalid_api_key"),
    403: ("invalid_request_error", "forbidden"),
    404: ("invalid_request_error", "not_found"),
    422: ("invalid_request_error", "unprocessable_entity"),
    429: ("rate_limit_error", "rate_limit_exceeded"),
    500: ("server_error", "internal_server_error"),
    503: ("server_error", "service_unavailable"),
}


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
            )
        else:
            checks = (("repo_readable", False, f"repository {source.repo} not found"),)
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=checks)

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return source.mirror_repo or source.repo

    async def verify_mirror(self, source: HFSource) -> bool:
        return True


class FakeBeamInferenceServer(FakeOpenAIServer):
    """The engine behind a Pod URL; ``inject_status`` answers the next chat calls with an error."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._forced: deque[int] = deque()

    def inject_status(self, status: int, *, count: int = 1) -> None:
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
            return JSONResponse(
                {
                    "error": {
                        "message": f"injected error {status}",
                        "type": error_type,
                        "code": code,
                    }
                },
                status_code=status,
            )
        return await super()._chat(request)


class FakeBeamGateway:
    """In-memory gateway REST API: deployments, their containers, scale, stop and delete."""

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.inference_url = ""
        self.deployments: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, int] = {}
        self.container_status = "RUNNING"
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def register(self, name: str) -> dict[str, Any]:
        """What ``Pod.deploy`` creates: an active deployment with no container yet."""
        self._counter += 1
        deployment_id = f"dep-{self._counter}"
        record = {"id": deployment_id, "name": name, "active": True, "stubId": f"stub-{name}"}
        self.deployments[deployment_id] = record
        self.containers[deployment_id] = 0
        return record

    @staticmethod
    def _ok(payload: Mapping[str, Any] | None = None) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, **(payload or {})})

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] | None = None
        if request.content:
            parsed = json.loads(request.content)
            body = parsed if isinstance(parsed, dict) else None
        path = request.url.path.removeprefix(GATEWAY_PREFIX)
        self.calls.append((request.method, path, body))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "invalid token"})
        if (request.method, path) == ("GET", "/deployments"):
            return self._ok({"deployments": list(self.deployments.values())})
        if (request.method, path) == ("GET", "/containers"):
            containers = [
                {
                    "containerId": f"{deployment_id}-c{index}",
                    "deploymentId": deployment_id,
                    "status": self.container_status,
                }
                for deployment_id, count in self.containers.items()
                for index in range(count)
            ]
            return self._ok({"containers": containers})
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "deployments":
            return httpx.Response(404, json={"message": "no such route"})
        deployment_id = parts[1]
        record = self.deployments.get(deployment_id)
        if record is None:
            return httpx.Response(200, json={"ok": False, "errMsg": "deployment not found"})
        action = parts[2] if len(parts) > 2 else ""
        if request.method == "POST" and action == "scale":
            self.containers[deployment_id] = int((body or {}).get("containers", 0))
            return self._ok()
        if request.method == "POST" and action == "stop":
            record["active"] = False
            self.containers[deployment_id] = 0
            return self._ok()
        if request.method == "DELETE" and not action:
            del self.deployments[deployment_id]
            self.containers.pop(deployment_id, None)
            return self._ok()
        return httpx.Response(405, json={"message": "method not allowed"})


class FakeBeamSdk:
    """Stands in for the ``beam`` module that ``launcher.deploy`` drives."""

    def __init__(self, gateway: FakeBeamGateway) -> None:
        self.gateway = gateway
        self.pods: list[dict[str, Any]] = []
        self.fail_with: str | None = None
        sdk = self

        class Image:
            @staticmethod
            def from_registry(uri: str) -> tuple[str, str]:
                return ("registry", uri)

        class Pod:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                sdk.pods.append(kwargs)

            def deploy(self, name: str) -> tuple[dict[str, Any], bool]:
                if sdk.fail_with is not None:
                    raise RuntimeError(sdk.fail_with)
                record = sdk.gateway.register(name)
                return {
                    "deployment_id": record["id"],
                    "stub_id": record["stubId"],
                    "invoke_url": sdk.gateway.inference_url,
                    "version": 1,
                }, True

        self.Image = Image
        self.Pod = Pod


class FakeLauncher:
    """A launch runner that runs ``launcher.deploy`` in-process against ``FakeBeamSdk``."""

    def __init__(self, sdk: FakeBeamSdk) -> None:
        self.sdk = sdk
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, argv: Sequence[str], stdin: bytes, env: Mapping[str, str], cwd: str, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        request = json.loads(stdin)
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd, "request": request})
        try:
            result = launcher.deploy(self.sdk, request)
        except RuntimeError as exc:
            result = {"ok": False, "error": f"RuntimeError: {exc}"}
        stdout = "=> Preparing image\n" + launcher.RESULT_PREFIX + json.dumps(result) + "\n"
        return (0 if result["ok"] else 1), stdout.encode(), b"sdk progress output\n"


def _spec() -> ModelSpec:
    return ModelSpec(
        slug=SLUG,
        spec_version=1,
        family="qwen3",
        roles=frozenset({"target"}),
        source=HFSource(repo=HF_PUBLIC_REPO, revision=_REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="beam-pod",
            account="conformance",
            params={"gpu": "RTX5090", "min_replicas": 1, "max_replicas": 1},
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="beam-pod",
        license="apache-2.0",
        owner="evalspleef",
    )


def _target() -> ConformanceTarget:
    server = FakeBeamInferenceServer(model=SLUG, api_key=TOKEN)
    gateway = FakeBeamGateway(token=TOKEN)
    sdk = FakeBeamSdk(gateway)
    fake_launcher = FakeLauncher(sdk)

    async def launch(
        argv: Sequence[str], stdin: bytes, env: Mapping[str, str], cwd: str, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        # The engine starts on first deploy; its address is only known then.
        server.start()
        gateway.inference_url = server.base_url.removesuffix("/v1")
        return await fake_launcher(argv, stdin, env, cwd, timeout_s)

    provider = BeamPodProvider(
        "conformance",
        {"api_key_ref": TOKEN_REF, "image": IMAGE},
        DictSecretResolver({TOKEN_REF: TOKEN, HF_TOKEN_REF: "hf_conformance"}),
        launch_runner=launch,
        api_transport=gateway.transport(),
    )
    checker = FakeHFChecker()
    provider.hf_checker = checker

    async def upstream_status(status: int, count: int = 1) -> None:
        server.inject_status(status, count=count)

    async def cleanup() -> None:
        server.stop()

    return ConformanceTarget(
        name="beam-pod-fake",
        kind="model",
        provider=provider,
        fake=True,
        injectors={"upstream_status": upstream_status},
        model_fixture=ModelFixture(
            spec_factory=_spec,
            operator_input_factory=None,
            upstream=server,
            hf_access=checker,
        ),
        notes=(
            "not verified by this target against a real Beam workspace: the SDK, the gateway REST "
            "API and the Pod are fakes (launcher.deploy runs in-process)"
        ),
        cleanup=cleanup,
    )


def build_targets() -> list[ConformanceTarget]:
    """The Beam target, running against fakes."""
    return [_target()]
