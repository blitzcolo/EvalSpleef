"""Model conformance cases (TECH-DESIGN section 15.1).

The library covers the model-side kit: ``M-API-01`` .. ``M-API-03``, ``M-ID-01``,
``M-LIFE-01`` .. ``M-LIFE-04``, ``M-ASSIST-01`` and ``M-HF-01``.

Applicability comes only from what the target actually supplies:

* API and identity cases need ``ModelFixture.upstream`` (scripted replies, injected
  statuses and the reported model name). A target without it reports not-applicable.
* ``M-API-03`` additionally needs the ``upstream_status`` fault injector, because the
  SPI has no inference proxy: the case drives the provider's ``wake`` and checks that
  upstream HTTP failures reach the caller as typed ``SpleefError`` exceptions.
* Lifecycle cases run only when the provider's ``management_mode`` is ``api``.
* ``M-ASSIST-01`` needs assisted mode, an operator input factory and the
  ``upstream_status`` injector for the vendor 403 path.
* ``M-HF-01`` needs ``ModelFixture.hf_access`` plus a Hugging Face source. The case
  probes the four fixture repos named by ``HF_*_REPO`` below; a target that supplies
  the HF double must answer for those repo names.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any

from evalspleef.conformance.case import Case
from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.errors import EndpointUnavailable, SpleefError
from evalspleef.spi.model import (
    EndpointHandle,
    EndpointState,
    HFAccess,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    UpstreamRoute,
)

_HTTP_TIMEOUT_S = 30.0

HF_PUBLIC_REPO = "evalspleef-conformance/public-model"
HF_GATED_REPO = "evalspleef-conformance/gated-model"
HF_PRIVATE_REPO = "evalspleef-conformance/private-model"
HF_BIN_ONLY_REPO = "evalspleef-conformance/bin-only-model"
"""Fixture repos ``M-HF-01`` probes; a supplied ``HFAccessChecker`` doubles these names."""

_HF_REVISION = "0" * 40


# --------------------------------------------------------------------------------------
# Target and provider helpers
# --------------------------------------------------------------------------------------


def _fixture(target: ConformanceTarget) -> ModelFixture:
    fixture = target.model_fixture
    assert fixture is not None, f"model target {target.name!r} has no model fixture"
    return fixture


def _provider(target: ConformanceTarget) -> ModelEndpointProvider:
    provider = target.provider
    assert isinstance(provider, ModelEndpointProvider), (
        f"model target {target.name!r} does not implement ModelEndpointProvider"
    )
    return provider


def _applies_model(target: ConformanceTarget) -> str | None:
    if target.kind != "model":
        return f"target kind is {target.kind!r}, not 'model'"
    if target.model_fixture is None:
        return "target supplies no model fixture"
    return None


def _applies_api(target: ConformanceTarget) -> str | None:
    reason = _applies_model(target)
    if reason is not None:
        return reason
    if _fixture(target).upstream is None:
        return "target supplies no upstream fixture for chat, streaming and identity checks"
    return None


def _applies_status_mapping(target: ConformanceTarget) -> str | None:
    reason = _applies_model(target)
    if reason is not None:
        return reason
    if "upstream_status" not in target.injectors:
        return "target supplies no 'upstream_status' injector for error-code mapping"
    return None


def _applies_lifecycle(target: ConformanceTarget) -> str | None:
    reason = _applies_model(target)
    if reason is not None:
        return reason
    mode = _provider(target).management_mode
    if mode is not ManagementMode.API:
        return f"management mode {mode.value!r}: lifecycle cases run for api mode only"
    return None


def _applies_assisted(target: ConformanceTarget) -> str | None:
    reason = _applies_model(target)
    if reason is not None:
        return reason
    mode = _provider(target).management_mode
    if mode is not ManagementMode.ASSISTED:
        return f"management mode {mode.value!r}: assisted flow runs for assisted mode only"
    if _fixture(target).operator_input_factory is None:
        return "assisted target supplies no operator input factory"
    if "upstream_status" not in target.injectors:
        return "assisted target supplies no 'upstream_status' injector for the 403 path"
    return None


def _applies_hf(target: ConformanceTarget) -> str | None:
    reason = _applies_model(target)
    if reason is not None:
        return reason
    fixture = _fixture(target)
    if fixture.hf_access is None:
        return "target supplies no HF access fixture"
    if not isinstance(fixture.spec_factory().source, HFSource):
        return "model source is not a Hugging Face repository"
    return None


async def _deployed(
    target: ConformanceTarget,
) -> tuple[ModelEndpointProvider, ModelFixture, ModelSpec, EndpointHandle]:
    """Validate, plan, deploy and (for assisted targets) attach one endpoint."""
    provider = _provider(target)
    fixture = _fixture(target)
    spec = fixture.spec_factory()
    plan = await provider.plan(spec)
    handle = await provider.deploy(spec, plan)
    if handle.state is EndpointState.AWAITING_OPERATOR:
        assert fixture.operator_input_factory is not None, (
            "provider deployed into AWAITING_OPERATOR but the target supplies no operator input"
        )
        handle = await provider.attach(handle, fixture.operator_input_factory())
    assert handle.state is not EndpointState.AWAITING_OPERATOR, (
        f"endpoint {handle.endpoint_id} is still awaiting the operator"
    )
    return provider, fixture, spec, handle


# --------------------------------------------------------------------------------------
# HTTP helpers (stdlib only: the conformance package must not depend on extra clients)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: bytes


async def _request(
    url: str,
    *,
    payload: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    method: str = "POST",
) -> _HttpResponse:
    def send() -> _HttpResponse:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                return _HttpResponse(status=response.status, body=response.read())
        except urllib.error.HTTPError as exc:
            return _HttpResponse(status=exc.code, body=exc.read())

    return await asyncio.to_thread(send)


def _chat_url(route: UpstreamRoute) -> str:
    return route.api_base.rstrip("/") + "/chat/completions"


def _headers(provider: ModelEndpointProvider, route: UpstreamRoute) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    headers.update(dict(route.extra_headers))
    try:
        key = provider.secrets.resolve(route.api_key_ref)
    except KeyError:
        key = ""
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


def _chat_payload(
    route: UpstreamRoute, *, stream: bool = False, max_tokens: int = 16
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": route.model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "conformance ping"}],
    }
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def _json_object(response: _HttpResponse, context: str) -> dict[str, Any]:
    try:
        body = json.loads(response.body)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{context}: response is not JSON ({exc}): {response.body[:200]!r}"
        ) from exc
    assert isinstance(body, dict), f"{context}: expected a JSON object, got {type(body).__name__}"
    return body


def _sse_events(body: bytes, context: str) -> tuple[list[dict[str, Any]], bool]:
    events: list[dict[str, Any]] = []
    done = False
    for line in body.decode("utf-8").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{context}: malformed SSE event {data!r} ({exc})") from exc
        assert isinstance(parsed, dict), f"{context}: SSE event is not an object: {data!r}"
        events.append(parsed)
    return events, done


async def _expect_spleef_error(awaitable: Awaitable[object], context: str) -> SpleefError:
    try:
        await awaitable
    except SpleefError as exc:
        return exc
    raise AssertionError(f"{context}: expected a typed SpleefError, the call succeeded")


# --------------------------------------------------------------------------------------
# M-API
# --------------------------------------------------------------------------------------


async def _api_chat(target: ConformanceTarget) -> None:
    provider, fixture, _spec, handle = await _deployed(target)
    upstream = fixture.upstream
    assert upstream is not None
    route = provider.upstream(handle)
    before = len(upstream.requests)

    response = await _request(
        _chat_url(route), payload=_chat_payload(route), headers=_headers(provider, route)
    )
    assert response.status == 200, f"M-API-01: chat returned HTTP {response.status}"
    body = _json_object(response, "M-API-01")
    choices = body.get("choices") or []
    assert choices, "M-API-01: chat response carries no choices"
    message = choices[0].get("message") or {}
    assert isinstance(message.get("content"), str) and message["content"], (
        "M-API-01: assistant message has no content"
    )
    usage = body.get("usage") or {}
    assert int(usage.get("prompt_tokens", 0)) > 0, "M-API-01: usage.prompt_tokens is missing"
    assert int(usage.get("total_tokens", 0)) >= int(usage.get("prompt_tokens", 0)), (
        "M-API-01: usage totals are inconsistent"
    )
    assert len(upstream.requests) == before + 1, "M-API-01: upstream did not record the request"
    assert upstream.requests[-1].body.get("model") == route.model, (
        f"M-API-01: sent model {upstream.requests[-1].body.get('model')!r}, "
        f"route model is {route.model!r}"
    )


async def _api_streaming(target: ConformanceTarget) -> None:
    provider, fixture, _spec, handle = await _deployed(target)
    assert fixture.upstream is not None
    route = provider.upstream(handle)

    response = await _request(
        _chat_url(route),
        payload=_chat_payload(route, stream=True, max_tokens=8),
        headers=_headers(provider, route),
    )
    assert response.status == 200, f"M-API-02: streaming chat returned HTTP {response.status}"
    events, done = _sse_events(response.body, "M-API-02")
    assert done, "M-API-02: stream has no [DONE] terminator"
    assert events, "M-API-02: stream carries no events"

    deltas = [
        choice["delta"]
        for event in events
        for choice in event.get("choices") or []
        if isinstance(choice.get("delta"), dict)
    ]
    content = "".join(str(delta.get("content") or "") for delta in deltas)
    assert content, "M-API-02: streamed content is empty"
    finish_reasons = [
        choice.get("finish_reason")
        for event in events
        for choice in event.get("choices") or []
        if choice.get("finish_reason")
    ]
    assert finish_reasons, "M-API-02: stream carries no finish reason"

    usage_events = [event["usage"] for event in events if isinstance(event.get("usage"), dict)]
    assert usage_events, "M-API-02: stream carries no usage record"
    usage = usage_events[-1]
    assert int(usage.get("prompt_tokens", 0)) > 0, "M-API-02: usage.prompt_tokens is missing"
    assert int(usage.get("completion_tokens", 0)) > 0, (
        "M-API-02: usage.completion_tokens is missing"
    )
    assert int(usage.get("total_tokens", 0)) == (
        int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
    ), "M-API-02: usage totals do not add up"


async def _api_error_mapping(target: ConformanceTarget) -> None:
    provider, _fixture, _spec, handle = await _deployed(target)
    injector = target.injectors["upstream_status"]

    expectations: tuple[tuple[int, bool], ...] = (
        (401, False),
        (404, False),
        (422, False),
        (429, True),
        (500, True),
        (503, True),
    )
    for status, retryable in expectations:
        await injector(status)
        error = await _expect_spleef_error(
            provider.wake(handle, timeout_s=60),
            f"M-API-03: upstream HTTP {status} was not surfaced as a typed error",
        )
        assert error.retryable is retryable, (
            f"M-API-03: upstream HTTP {status} mapped to {type(error).__name__} "
            f"(retryable={error.retryable}), expected retryable={retryable}"
        )
        if status in (429, 500, 503):
            assert isinstance(error, EndpointUnavailable), (
                f"M-API-03: upstream HTTP {status} mapped to {type(error).__name__}, "
                "expected EndpointUnavailable"
            )


# --------------------------------------------------------------------------------------
# M-ID
# --------------------------------------------------------------------------------------


async def _identity(target: ConformanceTarget) -> None:
    provider, _fixture, _spec, handle = await _deployed(target)
    route = provider.upstream(handle)
    response = await _request(
        _chat_url(route),
        payload=_chat_payload(route, max_tokens=4),
        headers=_headers(provider, route),
    )
    assert response.status == 200, f"M-ID-01: chat returned HTTP {response.status}"
    body = _json_object(response, "M-ID-01")
    reported = body.get("model")
    assert reported == route.model, (
        f"M-ID-01: response reports model {reported!r}, registered upstream model is {route.model!r}"
    )


# --------------------------------------------------------------------------------------
# M-LIFE
# --------------------------------------------------------------------------------------


async def _lifecycle_deploy(target: ConformanceTarget) -> None:
    provider, _fixture, spec, handle = await _deployed(target)
    assert handle.endpoint_id, "M-LIFE-01: deploy returned an endpoint without an endpoint_id"
    assert handle.model.slug == spec.slug, "M-LIFE-01: handle carries a different model spec"
    assert handle.state is EndpointState.READY, (
        f"M-LIFE-01: api-mode deploy ended in state {handle.state.value!r}, expected 'ready'"
    )
    assert handle.base_url, "M-LIFE-01: api-mode deploy returned no base_url"
    route = provider.upstream(handle)
    assert route.api_base == handle.base_url, (
        "M-LIFE-01: upstream route base differs from the deployed handle base_url"
    )
    assert route.model, "M-LIFE-01: upstream route names no model"


async def _lifecycle_status(target: ConformanceTarget) -> None:
    provider, _fixture, _spec, handle = await _deployed(target)
    first = await provider.status(handle)
    second = await provider.status(handle)
    assert isinstance(first, EndpointState), f"M-LIFE-02: status returned {first!r}"
    assert first is second, (
        f"M-LIFE-02: status is not stable: {first.value!r} then {second.value!r}"
    )
    assert first is handle.state, (
        f"M-LIFE-02: status reports {first.value!r}, handle state is {handle.state.value!r}"
    )


async def _lifecycle_scale(target: ConformanceTarget) -> None:
    provider, _fixture, _spec, handle = await _deployed(target)
    route = provider.upstream(handle)
    await provider.scale(handle, 1, 2)
    state = await provider.status(handle)
    assert isinstance(state, EndpointState), f"M-LIFE-03: scale left an invalid state {state!r}"
    scaled = provider.upstream(handle)
    assert scaled.api_base == route.api_base, "M-LIFE-03: scale changed the upstream base_url"
    assert scaled.model == route.model, "M-LIFE-03: scale changed the upstream model"


async def _lifecycle_teardown(target: ConformanceTarget) -> None:
    provider, _fixture, _spec, handle = await _deployed(target)
    await provider.teardown(handle, "terminate")
    state = await provider.status(handle)
    assert state is EndpointState.RETIRED, (
        f"M-LIFE-04: teardown('terminate') left the endpoint in {state.value!r}, expected 'retired'"
    )


# --------------------------------------------------------------------------------------
# M-ASSIST
# --------------------------------------------------------------------------------------


async def _assisted_flow(target: ConformanceTarget) -> None:
    provider = _provider(target)
    fixture = _fixture(target)
    factory = fixture.operator_input_factory
    assert factory is not None
    injector = target.injectors["upstream_status"]
    spec = fixture.spec_factory()

    report = await provider.validate(spec)
    assert report.ok, f"M-ASSIST-01: validate rejected {spec.slug}: {report.checks}"
    plan = await provider.plan(spec)
    instructions = plan.operator_instructions_md
    assert instructions, "M-ASSIST-01: plan produced no operator instructions"
    assert spec.slug in instructions, (
        "M-ASSIST-01: operator instructions do not name the model slug"
    )

    handle = await provider.deploy(spec, plan)
    assert handle.state is EndpointState.AWAITING_OPERATOR, (
        f"M-ASSIST-01: assisted deploy ended in {handle.state.value!r}, "
        "expected 'awaiting_operator'"
    )
    assert handle.base_url is None, "M-ASSIST-01: assisted deploy must not invent a base_url"

    operator_input = dict(factory())
    assert operator_input.get("base_url"), "M-ASSIST-01: operator input has no base_url"
    assert operator_input.get("model"), "M-ASSIST-01: operator input has no model"

    attached = await provider.attach(handle, operator_input)
    assert attached.state is not EndpointState.AWAITING_OPERATOR, (
        "M-ASSIST-01: successful attach left the endpoint awaiting the operator"
    )
    route = provider.upstream(attached)
    assert route.api_base == operator_input["base_url"], (
        "M-ASSIST-01: upstream route does not use the attached base_url"
    )
    assert route.model == operator_input["model"], (
        "M-ASSIST-01: upstream route does not use the attached model name"
    )

    wrong_base = dict(operator_input)
    wrong_base["base_url"] = "http://operator.example/v1"
    bad_handle = await provider.deploy(spec, plan)
    await _expect_spleef_error(
        provider.attach(bad_handle, wrong_base),
        "M-ASSIST-01: attach accepted a plain-http base_url",
    )
    assert bad_handle.state is EndpointState.AWAITING_OPERATOR, (
        "M-ASSIST-01: rejected base_url moved the endpoint out of awaiting_operator"
    )
    assert bad_handle.base_url is None, "M-ASSIST-01: rejected base_url was stored on the handle"

    wrong_model = dict(operator_input)
    wrong_model["model"] = f"not-{operator_input['model']}"
    model_handle = await provider.deploy(spec, plan)
    await _expect_spleef_error(
        provider.attach(model_handle, wrong_model),
        "M-ASSIST-01: attach accepted a model name that does not match the registration",
    )
    assert model_handle.state is EndpointState.AWAITING_OPERATOR, (
        "M-ASSIST-01: rejected model moved the endpoint out of awaiting_operator"
    )

    forbidden_handle = await provider.deploy(spec, plan)
    await injector(403)
    error = await _expect_spleef_error(
        provider.attach(forbidden_handle, operator_input),
        "M-ASSIST-01: attach accepted a deployment the vendor answers 403 for",
    )
    assert error.retryable is False, (
        f"M-ASSIST-01: 403 attach mapped to a retryable {type(error).__name__}, expected a "
        "terminal failure"
    )
    assert forbidden_handle.state is EndpointState.AWAITING_OPERATOR, (
        "M-ASSIST-01: 403 attach moved the endpoint out of awaiting_operator"
    )


# --------------------------------------------------------------------------------------
# M-HF
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _HFScenario:
    name: str
    repo: str
    access: HFAccess
    token_ref: str | None
    reasons: tuple[str, ...]
    """Substrings (lower case) the failed checks must mention for a negative scenario."""


_HF_SCENARIOS: tuple[_HFScenario, ...] = (
    _HFScenario("public", HF_PUBLIC_REPO, "public", None, ()),
    _HFScenario("gated", HF_GATED_REPO, "gated", None, ("gated", "licen")),
    _HFScenario(
        "private",
        HF_PRIVATE_REPO,
        "private",
        "vault://hf/conformance",
        ("private", "permission", "access"),
    ),
    _HFScenario("bin_only", HF_BIN_ONLY_REPO, "public", None, (".bin", "safetensors")),
)


async def _hf_preflight(target: ConformanceTarget) -> None:
    checker = _fixture(target).hf_access
    assert checker is not None
    for scenario in _HF_SCENARIOS:
        source = HFSource(
            repo=scenario.repo,
            revision=_HF_REVISION,
            access=scenario.access,
            token_ref=scenario.token_ref,
        )
        report = await checker.preflight(source)
        if not scenario.reasons:
            assert report.ok, (
                f"M-HF-01: preflight rejected the public repo {scenario.repo}: {report.checks}"
            )
            assert report.checks, "M-HF-01: public preflight returned no checks"
            assert all(passed for _check_id, passed, _detail in report.checks), (
                "M-HF-01: public preflight reports ok but carries failed checks"
            )
            continue

        assert not report.ok, f"M-HF-01: preflight accepted the {scenario.name} scenario"
        failed = [check for check in report.checks if not check[1]]
        assert failed, f"M-HF-01: {scenario.name} preflight failed without naming a check"
        details = " ".join(detail for _check_id, _passed, detail in failed).lower()
        assert all(detail.strip() for _check_id, _passed, detail in failed), (
            f"M-HF-01: {scenario.name} preflight failed without a reason"
        )
        assert any(reason in details for reason in scenario.reasons), (
            f"M-HF-01: {scenario.name} failure does not explain the cause; details={details!r}"
        )


# --------------------------------------------------------------------------------------
# The case library
# --------------------------------------------------------------------------------------

CASES: list[Case] = [
    Case(
        id="M-API-01",
        kind="model",
        title="chat completion returns a message and token usage",
        applies=_applies_api,
        run=_api_chat,
    ),
    Case(
        id="M-API-02",
        kind="model",
        title="streaming chat delivers content, finish reason and usage",
        applies=_applies_api,
        run=_api_streaming,
    ),
    Case(
        id="M-API-03",
        kind="model",
        title="upstream 401/404/422/429/5xx map to typed errors with the right retryability",
        applies=_applies_status_mapping,
        run=_api_error_mapping,
    ),
    Case(
        id="M-ID-01",
        kind="model",
        title="the response model field matches the registered upstream model",
        applies=_applies_api,
        run=_identity,
    ),
    Case(
        id="M-LIFE-01",
        kind="model",
        title="api-mode deploy returns a ready handle whose route matches",
        applies=_applies_lifecycle,
        run=_lifecycle_deploy,
    ),
    Case(
        id="M-LIFE-02",
        kind="model",
        title="status reports the handle state consistently",
        applies=_applies_lifecycle,
        run=_lifecycle_status,
    ),
    Case(
        id="M-LIFE-03",
        kind="model",
        title="scale keeps the upstream route stable",
        applies=_applies_lifecycle,
        run=_lifecycle_scale,
    ),
    Case(
        id="M-LIFE-04",
        kind="model",
        title="teardown terminates the endpoint",
        applies=_applies_lifecycle,
        run=_lifecycle_teardown,
    ),
    Case(
        id="M-ASSIST-01",
        kind="model",
        title="assisted flow: instructions, attach, and rejected base_url/model/403 paths",
        applies=_applies_assisted,
        run=_assisted_flow,
    ),
    Case(
        id="M-HF-01",
        kind="model",
        title="HF preflight explains gated, private and .bin-only failures",
        applies=_applies_hf,
        run=_hf_preflight,
    ),
]
