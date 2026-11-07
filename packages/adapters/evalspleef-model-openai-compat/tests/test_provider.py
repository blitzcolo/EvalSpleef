"""Provider tests against the real HTTP fake OpenAI server and injected transports."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import httpx
import pytest
from evalspleef_model_openai_compat.provider import OpenAICompatibleProvider

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.model import (
    DeploymentSpec,
    EndpointHandle,
    EndpointState,
    ModelSpec,
    ProviderLock,
    VendorSource,
)
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver

_SECRET_REF = "vault://models/test"
_API_KEY = "sk-test"
_BASE_URL = "http://upstream.test/v1"


def make_spec(base_url: str = _BASE_URL, **params: Any) -> ModelSpec:
    request_params: dict[str, Any] = {
        "base_url": base_url,
        "model": "fake-model",
        "api_key_ref": _SECRET_REF,
        **params,
    }
    return ModelSpec(
        slug="test-model",
        spec_version=1,
        family="test",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="test", model_id="fake-model"),
        deployment=DeploymentSpec(
            adapter="openai-compatible",
            account="test",
            params=request_params,
        ),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="test",
        license="proprietary",
        owner="evalspleef",
    )


def make_provider(
    config: dict[str, Any] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    secrets: DictSecretResolver | None = None,
) -> OpenAICompatibleProvider:
    merged: dict[str, Any] = {"api_key_ref": _SECRET_REF, "wake_retry_interval_s": 0.01}
    merged.update(config or {})
    return OpenAICompatibleProvider(
        "test",
        merged,
        secrets if secrets is not None else DictSecretResolver({_SECRET_REF: _API_KEY}),
        transport=transport,
    )


async def deploy(provider: OpenAICompatibleProvider, spec: ModelSpec) -> EndpointHandle:
    return await provider.deploy(spec, await provider.plan(spec))


@pytest.fixture
async def server() -> AsyncIterator[FakeOpenAIServer]:
    async with FakeOpenAIServer(model="fake-model", api_key=_API_KEY) as running:
        yield running


def transport_with(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# -- validate ------------------------------------------------------------------


async def test_validate_against_real_http_fake(server: FakeOpenAIServer) -> None:
    report = await make_provider().validate(make_spec(server.base_url))

    assert report.ok is True
    checks = dict((check_id, passed) for check_id, passed, _detail in report.checks)
    assert checks["credential"] is True
    assert checks["model_exists"] is True


async def test_validate_missing_secret_fails_credential_check() -> None:
    provider = make_provider(secrets=DictSecretResolver({}))

    report = await provider.validate(make_spec())

    assert report.ok is False
    detail = {check_id: (passed, text) for check_id, passed, text in report.checks}
    assert detail["credential"][0] is False
    assert "cannot resolve" in detail["credential"][1]


async def test_validate_404_on_models_is_tolerated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(404, json={"error": "not found"})

    report = await make_provider(transport=transport_with(handler)).validate(make_spec())

    assert report.ok is True
    detail = {check_id: (passed, text) for check_id, passed, text in report.checks}
    assert detail["models_endpoint"][0] is True
    assert "404" in detail["models_endpoint"][1]


async def test_validate_401_is_a_failed_check_with_clear_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    report = await make_provider(transport=transport_with(handler)).validate(make_spec())

    assert report.ok is False
    detail = {check_id: (passed, text) for check_id, passed, text in report.checks}
    assert detail["credential"][0] is False
    assert "401" in detail["credential"][1]


async def test_validate_model_absent_from_models_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "another-model"}]})

    report = await make_provider(transport=transport_with(handler)).validate(make_spec())

    assert report.ok is False
    detail = {check_id: (passed, text) for check_id, passed, text in report.checks}
    assert detail["model_exists"][0] is False
    assert "not found" in detail["model_exists"][1]


async def test_validate_rejects_unknown_protocol() -> None:
    report = await make_provider().validate(make_spec(protocol="grpc"))

    assert report.ok is False
    detail = {check_id: passed for check_id, passed, _text in report.checks}
    assert detail["protocol"] is False


async def test_validate_rejects_malformed_provider_lock() -> None:
    provider = make_provider({"provider_lock": {"response_field": "provider"}})

    report = await provider.validate(make_spec())

    assert report.ok is False
    detail = {check_id: passed for check_id, passed, _text in report.checks}
    assert detail["provider_lock"] is False


# -- deploy / status -----------------------------------------------------------


async def test_deploy_returns_verifying_handle() -> None:
    handle = await deploy(make_provider(), make_spec())

    assert handle.state is EndpointState.VERIFYING
    assert handle.base_url == _BASE_URL
    assert handle.upstream_model == "fake-model"
    assert handle.protocol == "chat_completions"
    assert handle.credential_ref == _SECRET_REF
    assert handle.endpoint_id.startswith("ep_test-model_")


async def test_deploy_requires_base_url() -> None:
    spec = ModelSpec(
        slug="no-url",
        spec_version=1,
        family="test",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="test", model_id="fake-model"),
        deployment=DeploymentSpec(adapter="openai-compatible", account="test", params={}),
        declared_context_tokens=1,
        declared_max_output_tokens=1,
        pricing_ref="test",
        license="proprietary",
        owner="evalspleef",
    )
    with pytest.raises(EndpointUnavailable):
        await deploy(make_provider(), spec)


async def test_status_ready_after_single_token_health_request(
    server: FakeOpenAIServer,
) -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec(server.base_url))

    assert await provider.status(handle) is EndpointState.READY
    assert handle.state is EndpointState.READY
    assert server.requests[-1].body["max_tokens"] == 1
    assert server.requests[-1].body["model"] == "fake-model"


async def test_status_reports_warm_when_throttled(server: FakeOpenAIServer) -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec(server.base_url))
    server.inject_error(429)

    assert await provider.status(handle) is EndpointState.WARM


async def test_status_failed_on_server_error(server: FakeOpenAIServer) -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec(server.base_url))
    server.inject_error(500)

    assert await provider.status(handle) is EndpointState.FAILED
    assert "HTTP 500" in handle.observed["last_health"]


async def test_status_failed_on_model_mismatch(server: FakeOpenAIServer) -> None:
    server.reported_model = "some-other-model"
    provider = make_provider()
    handle = await deploy(provider, make_spec(server.base_url))

    assert await provider.status(handle) is EndpointState.FAILED
    assert "model mismatch" in handle.observed["last_health"]


# -- wake / teardown -----------------------------------------------------------


async def test_wake_returns_warm_on_success(server: FakeOpenAIServer) -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec(server.base_url))

    assert await provider.wake(handle, timeout_s=5) is EndpointState.WARM
    assert handle.observed["wake_attempts"] == 1


@pytest.mark.parametrize(
    ("status", "error_cls", "retryable"),
    [
        (401, EndpointMismatch, False),
        (404, EndpointMismatch, False),
        (422, EndpointMismatch, False),
        (429, EndpointUnavailable, True),
        (500, EndpointUnavailable, True),
        (503, EndpointUnavailable, True),
    ],
)
async def test_wake_maps_upstream_status(
    status: int,
    error_cls: type[EndpointMismatch | EndpointUnavailable],
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "injected"})

    provider = make_provider(transport=transport_with(handler))
    handle = await deploy(provider, make_spec())

    with pytest.raises(error_cls) as raised:
        await provider.wake(handle, timeout_s=60)
    assert raised.value.retryable is retryable
    assert str(status) in (raised.value.message + str(raised.value.detail))


async def test_wake_retries_connection_failures_until_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("host vanished", request=request)

    provider = make_provider(transport=transport_with(handler))
    handle = await deploy(provider, make_spec())

    with pytest.raises(EndpointUnavailable):
        await provider.wake(handle, timeout_s=0.05)
    assert attempts >= 2


@pytest.mark.parametrize("mode", ["sleep", "terminate"])
async def test_teardown_marks_retired(mode: Literal["sleep", "terminate"]) -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec())

    await provider.teardown(handle, mode)

    assert handle.state is EndpointState.RETIRED
    assert await provider.status(handle) is EndpointState.RETIRED


# -- upstream ------------------------------------------------------------------


async def test_upstream_route_carries_lock_limits_and_headers() -> None:
    provider = make_provider(
        {
            "provider_lock": {"response_field": "provider", "pattern": "^novita$"},
            "extra_headers": {"X-Eval": "spleef"},
            "default_params": {"temperature": 0},
            "rpm": 60,
            "tpm": 1000,
            "cold_start_window_s": 30,
        }
    )
    handle = await deploy(provider, make_spec())

    route = provider.upstream(handle)

    assert route.api_base == _BASE_URL
    assert route.model == "fake-model"
    assert route.protocol == "chat_completions"
    assert route.api_key_ref == _SECRET_REF
    assert route.default_params == {"temperature": 0, "rpm": 60, "tpm": 1000}
    assert route.extra_headers == {"X-Eval": "spleef"}
    assert route.provider_lock == ProviderLock(response_field="provider", pattern="^novita$")
    assert route.cold_start_window_s == 30


async def test_upstream_requires_base_url() -> None:
    provider = make_provider()
    handle = await deploy(provider, make_spec())
    handle.base_url = None

    with pytest.raises(EndpointUnavailable):
        provider.upstream(handle)


# -- protocols -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "path", "token_field"),
    [
        ("chat_completions", "/v1/chat/completions", "max_tokens"),
        ("responses", "/v1/responses", "max_output_tokens"),
        ("anthropic_messages", "/v1/messages", "max_tokens"),
    ],
)
async def test_health_request_uses_protocol_path_and_body(
    protocol: str, path: str, token_field: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "fake-model"})

    provider = make_provider(transport=transport_with(handler))
    spec = make_spec(protocol=protocol)
    handle = await deploy(provider, spec)

    assert await provider.status(handle) is EndpointState.READY
    request = seen[-1]
    assert request.url.path == path
    header = "x-api-key" if protocol == "anthropic_messages" else "authorization"
    assert request.headers[header] == (_API_KEY if header == "x-api-key" else f"Bearer {_API_KEY}")
    body = json.loads(request.content)
    assert body[token_field] == 1
    assert body["model"] == "fake-model"


async def test_deploy_rejects_unknown_protocol() -> None:
    with pytest.raises(EndpointUnavailable):
        await deploy(make_provider(), make_spec(protocol="grpc"))
