"""Behavioural tests for the mock model endpoint providers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.model import (
    DeploymentSpec,
    EndpointHandle,
    EndpointState,
    HFSource,
    ModelSpec,
    UpstreamRoute,
)
from evalspleef.spi.secrets import SecretRef
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_endpoint import (
    MockApiProvider,
    MockAssistedProvider,
    MockExternalProvider,
)

SECRETS = {"vault://models/mock-32b": "secret-value"}


class DictSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, ref: SecretRef | str) -> str:
        return self._values[str(ref)]


def _spec(params: dict[str, Any] | None = None) -> ModelSpec:
    return ModelSpec(
        slug="mock-32b",
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=HFSource(repo="org/mock-32b", revision="0" * 40, access="public"),
        deployment=DeploymentSpec(adapter="mock-assisted", account="acct", params=params or {}),
        declared_context_tokens=32768,
        declared_max_output_tokens=4096,
        pricing_ref="ref:mock/32b",
        license="apache-2.0",
        owner="qa",
    )


def _assisted(
    clock: FakeClock | None = None, config: dict[str, Any] | None = None
) -> MockAssistedProvider:
    base_config: dict[str, Any] = {
        "model": "mock-32b-upstream",
        "api_key_ref": "vault://models/mock-32b",
    }
    base_config.update(config or {})
    return MockAssistedProvider(
        "acct", base_config, DictSecrets(SECRETS), clock=clock or FakeClock()
    )


def _external(config: dict[str, Any] | None = None) -> MockExternalProvider:
    base_config: dict[str, Any] = {
        "base_url": "https://api.example.test/v1",
        "model": "mock-32b-upstream",
        "api_key_ref": "vault://models/mock-32b",
    }
    base_config.update(config or {})
    return MockExternalProvider("acct", base_config, DictSecrets(SECRETS))


async def _attached_assisted(
    clock: FakeClock | None = None,
) -> tuple[MockAssistedProvider, EndpointHandle]:
    provider = _assisted(clock=clock)
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    handle = await provider.attach(
        handle,
        {
            "base_url": "https://ep-123.novita.ai/v1",
            "model": "mock-32b-upstream",
            "deployment_id": "dep_123",
        },
    )
    return provider, handle


async def test_assisted_deploy_awaits_operator_with_instructions() -> None:
    provider = _assisted()
    spec = _spec()
    plan = await provider.plan(spec)

    handle = await provider.deploy(spec, plan)

    assert handle.state is EndpointState.AWAITING_OPERATOR
    assert handle.base_url is None
    assert plan.operator_instructions_md
    assert handle.observed["operator_instructions_md"] == plan.operator_instructions_md


async def test_assisted_attach_rejects_http_base_url() -> None:
    provider = _assisted()
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))

    with pytest.raises(EndpointMismatch, match="https"):
        await provider.attach(
            handle,
            {"base_url": "http://ep-123.novita.ai/v1", "model": "mock-32b-upstream"},
        )

    assert handle.state is EndpointState.AWAITING_OPERATOR
    assert handle.base_url is None


async def test_assisted_attach_rejects_wrong_model_name() -> None:
    provider = _assisted()
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))

    with pytest.raises(EndpointMismatch, match="model name"):
        await provider.attach(
            handle,
            {"base_url": "https://ep-123.novita.ai/v1", "model": "someone-else"},
        )

    assert handle.state is EndpointState.AWAITING_OPERATOR
    assert handle.upstream_model is None


async def test_assisted_attach_accepts_valid_input_and_moves_to_verifying() -> None:
    _provider, handle = await _attached_assisted()

    assert handle.state is EndpointState.VERIFYING
    assert handle.base_url == "https://ep-123.novita.ai/v1"
    assert handle.upstream_model == "mock-32b-upstream"
    assert handle.observed["deployment_id"] == "dep_123"


async def test_assisted_wake_simulates_cold_start_on_the_injected_clock() -> None:
    clock = FakeClock(start=100.0)
    provider, handle = await _attached_assisted(clock)
    provider.config["cold_start_s"] = 300.0

    task = asyncio.create_task(provider.wake(handle, timeout_s=600))
    await asyncio.sleep(0)

    assert handle.state is EndpointState.SLEEPING
    assert clock.pending_sleepers == 1

    clock.advance(299.0)
    await asyncio.sleep(0)
    assert not task.done()

    clock.advance(1.0)
    async with asyncio.timeout(2):
        state = await task
    assert state is EndpointState.WARM
    assert handle.state is EndpointState.WARM
    assert handle.observed["cold_start_s"] == 300.0
    assert clock.now() == 400.0
    assert clock.pending_sleepers == 0


async def test_assisted_wake_gives_up_without_sleeping_past_the_timeout() -> None:
    clock = FakeClock()
    provider, handle = await _attached_assisted(clock)
    provider.config["cold_start_s"] = 300.0

    state = await provider.wake(handle, timeout_s=60)

    assert state is EndpointState.SLEEPING
    assert handle.state is EndpointState.SLEEPING
    assert clock.pending_sleepers == 0


async def test_assisted_wake_requires_an_attached_endpoint() -> None:
    provider = _assisted()
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))

    with pytest.raises(EndpointUnavailable, match="base_url"):
        await provider.wake(handle, timeout_s=60)


async def test_assisted_scale_and_teardown_only_open_tickets() -> None:
    provider, handle = await _attached_assisted()

    await provider.scale(handle, 2, 4)
    await provider.teardown(handle, "terminate")

    assert handle.observed["pending_scale"] == {"min_replicas": 2, "max_replicas": 4}
    assert handle.observed["pending_teardown"] == "terminate"
    assert handle.state is EndpointState.VERIFYING


async def test_api_scale_and_teardown_change_state_immediately() -> None:
    provider = MockApiProvider("acct", {"model": "mock-32b-upstream"}, DictSecrets(SECRETS))
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    assert handle.state is EndpointState.READY

    await provider.scale(handle, 1, 3)
    assert handle.state is EndpointState.WARM
    assert handle.observed["min_replicas"] == 1
    assert handle.observed["max_replicas"] == 3

    await provider.scale(handle, 0, 0)
    assert handle.state is EndpointState.SLEEPING

    await provider.teardown(handle, "sleep")
    assert handle.state is EndpointState.SLEEPING

    await provider.teardown(handle, "terminate")
    assert handle.state is EndpointState.RETIRED
    assert await provider.status(handle) is EndpointState.RETIRED


async def test_external_teardown_only_marks_retired() -> None:
    provider = _external()
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))
    assert handle.state is EndpointState.VERIFYING
    assert handle.base_url == "https://api.example.test/v1"

    assert await provider.wake(handle, timeout_s=30) is EndpointState.WARM
    base_url_before = handle.base_url

    await provider.teardown(handle, "terminate")

    assert handle.state is EndpointState.RETIRED
    assert handle.base_url == base_url_before
    assert handle.observed == {}


async def test_upstream_route_is_built_from_the_handle_and_config() -> None:
    provider = _external(
        {
            "default_params": {"max_tokens": 1024},
            "extra_headers": {"x-tenant": "qa"},
            "provider_lock": {"response_field": "provider", "pattern": "^mock$"},
            "cold_start_window_s": 120,
        }
    )
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))

    route = provider.upstream(handle)

    assert isinstance(route, UpstreamRoute)
    assert route.api_base == "https://api.example.test/v1"
    assert route.model == "mock-32b-upstream"
    assert route.protocol == "chat_completions"
    assert route.api_key_ref == "vault://models/mock-32b"
    assert route.default_params == {"max_tokens": 1024}
    assert route.extra_headers == {"x-tenant": "qa"}
    assert route.provider_lock is not None
    assert route.provider_lock.response_field == "provider"
    assert route.cold_start_window_s == 120


async def test_upstream_requires_a_base_url() -> None:
    provider = _assisted()
    spec = _spec()
    handle = await provider.deploy(spec, await provider.plan(spec))

    with pytest.raises(EndpointUnavailable, match="base_url"):
        provider.upstream(handle)


async def test_validate_flags_a_non_https_external_base_url() -> None:
    provider = _external({"base_url": "http://api.example.test/v1"})

    report = await provider.validate(_spec())

    checks = {check_id: passed for check_id, passed, _ in report.checks}
    assert report.ok is False
    assert checks["base_url_https"] is False


async def test_validate_passes_for_a_well_configured_api_provider() -> None:
    provider = MockApiProvider(
        "acct",
        {
            "base_url": "https://api.example.test/v1",
            "model": "mock-32b-upstream",
            "api_key_ref": "vault://models/mock-32b",
        },
        DictSecrets(SECRETS),
    )

    report = await provider.validate(_spec())

    assert report.ok is True
    assert all(passed for _, passed, _ in report.checks)
