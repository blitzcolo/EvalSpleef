"""OpenAI-compatible model endpoint provider (TECH-DESIGN section 6.6.1, PRD FR-MOD-04).

External management mode: the endpoint already exists (a vendor API, Novita Serverless,
self-hosted vLLM/SGLang or an aggregator) and the provider only registers it, verifies it
with a minimal health request and hands the gateway its upstream route.

Configuration (provider config, overridable per ``ModelSpec.deployment.params``):

- ``base_url``: the upstream OpenAI-compatible base URL (usually ending in ``/v1``);
- ``model``: the ``model`` field sent upstream and expected back;
- ``api_key_ref``: a ``SecretRef`` resolved through the injected resolver;
- ``extra_headers``: additional request headers;
- ``protocol``: ``chat_completions`` (default), ``responses`` or ``anthropic_messages``;
- ``rpm`` / ``tpm``: rate limits carried on the gateway route;
- ``provider_lock``: ``{response_field, pattern}`` for aggregator vendor locking;
- ``default_params``: extra default parameters for the gateway route.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    ProviderLock,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
    WireProtocol,
)
from evalspleef.spi.secrets import SecretResolver

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_WAKE_RETRY_INTERVAL_S = 1.0

_PROTOCOLS: tuple[WireProtocol, ...] = (
    "chat_completions",
    "responses",
    "anthropic_messages",
)
_HEALTH_PATHS: dict[WireProtocol, str] = {
    "chat_completions": "chat/completions",
    "responses": "responses",
    "anthropic_messages": "messages",
}


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _endpoint_id(spec: ModelSpec) -> str:
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_provider_lock(raw: object) -> ProviderLock | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("provider_lock must be a mapping with response_field and pattern")
    response_field = raw.get("response_field")
    pattern = raw.get("pattern")
    if not isinstance(response_field, str) or not isinstance(pattern, str):
        raise ValueError("provider_lock requires string response_field and pattern fields")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"provider_lock pattern is not a valid regex: {exc}") from exc
    return ProviderLock(response_field=response_field, pattern=pattern)


def _protocol(value: object) -> WireProtocol | None:
    if isinstance(value, str) and value in _PROTOCOLS:
        return value
    return None


class OpenAICompatibleProvider(ModelEndpointProvider):
    """Registers an existing OpenAI-compatible endpoint; never manages its lifecycle."""

    name = "openai-compatible"
    management_mode = ManagementMode.EXTERNAL

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._transport = transport
        self._timeout_s = (
            timeout_s
            if timeout_s is not None
            else float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        )
        self._wake_retry_interval_s = max(
            0.0, float(config.get("wake_retry_interval_s", DEFAULT_WAKE_RETRY_INTERVAL_S))
        )

    # -- configuration helpers -------------------------------------------------

    def _base_url(self, spec: ModelSpec) -> str | None:
        value = self.config.get("base_url", spec.deployment.params.get("base_url"))
        return value if isinstance(value, str) and value else None

    def _model(self, spec: ModelSpec) -> str | None:
        value = self.config.get("model", spec.deployment.params.get("model"))
        return value if isinstance(value, str) and value else None

    def _credential_ref(self, spec: ModelSpec) -> str | None:
        value = self.config.get("api_key_ref", spec.deployment.params.get("api_key_ref"))
        return value if isinstance(value, str) and value else None

    def _protocol(self, spec: ModelSpec) -> WireProtocol:
        raw = self.config.get("protocol", spec.deployment.params.get("protocol"))
        resolved = _protocol(raw if raw is not None else "chat_completions")
        if resolved is None:
            raise EndpointUnavailable(
                f"unsupported protocol {raw!r}; expected one of {', '.join(_PROTOCOLS)}"
            )
        return resolved

    def _extra_headers(self) -> dict[str, str]:
        return {
            str(key): str(value)
            for key, value in _mapping(self.config.get("extra_headers")).items()
        }

    def _provider_lock(self) -> ProviderLock | None:
        return _parse_provider_lock(self.config.get("provider_lock"))

    def _auth_headers(self, key: str, protocol: WireProtocol) -> dict[str, str]:
        if protocol == "anthropic_messages":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            headers = {"Authorization": f"Bearer {key}"}
        headers.update(self._extra_headers())
        return headers

    async def _get(self, url: str, *, key: str, protocol: WireProtocol) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s), transport=self._transport
        ) as client:
            return await client.get(url, headers=self._auth_headers(key, protocol))

    async def _post(
        self, url: str, *, key: str, protocol: WireProtocol, json: Mapping[str, Any]
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s), transport=self._transport
        ) as client:
            return await client.post(url, headers=self._auth_headers(key, protocol), json=json)

    def _health_body(self, model: str, protocol: WireProtocol) -> dict[str, Any]:
        if protocol == "responses":
            return {"model": model, "input": "ping", "max_output_tokens": 1}
        return {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }

    # -- SPI -------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        """Credential, configuration and (when supported) ``GET /models`` checks."""
        checks: list[tuple[str, bool, str]] = []
        base_url = self._base_url(spec)
        checks.append(
            (
                "base_url",
                base_url is not None and base_url.startswith(("http://", "https://")),
                base_url or "missing base_url",
            )
        )
        model = self._model(spec)
        checks.append(("model", model is not None, model or "missing model"))

        credential_ref = self._credential_ref(spec)
        key: str | None = None
        if credential_ref is None:
            checks.append(("credential", False, "missing api_key_ref"))
        else:
            try:
                key = self.secrets.resolve(credential_ref)
            except (KeyError, ValueError) as exc:
                checks.append(("credential", False, f"cannot resolve {credential_ref}: {exc}"))
            else:
                checks.append(("credential", True, f"resolved {credential_ref}"))

        protocol: WireProtocol | None
        try:
            protocol = self._protocol(spec)
        except EndpointUnavailable as exc:
            protocol = None
            checks.append(("protocol", False, exc.message))
        else:
            checks.append(("protocol", True, protocol))

        try:
            self._provider_lock()
        except ValueError as exc:
            checks.append(("provider_lock", False, str(exc)))
        else:
            checks.append(("provider_lock", True, "valid or absent"))

        if base_url is not None and key is not None and protocol is not None:
            try:
                response = await self._get(_join(base_url, "models"), key=key, protocol=protocol)
            except httpx.HTTPError as exc:
                checks.append(("models_endpoint", False, f"GET /models failed: {exc}"))
            else:
                checks.append(self._models_check(response, model))

        return ValidationReport(
            ok=all(passed for _check_id, passed, _detail in checks), checks=tuple(checks)
        )

    @staticmethod
    def _models_check(response: httpx.Response, model: str | None) -> tuple[str, bool, str]:
        status = response.status_code
        if status == 404:
            return ("models_endpoint", True, "GET /models unsupported (404); model not checked")
        if status == 401:
            return ("credential", False, "credential rejected by upstream (HTTP 401)")
        if not response.is_success:
            return ("models_endpoint", False, f"GET /models returned HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            return ("models_endpoint", False, "GET /models returned invalid JSON")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            return ("models_endpoint", False, "GET /models response has no data list")
        advertised = {
            entry.get("id")
            for entry in data
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
        }
        found = model in advertised
        return (
            "model_exists",
            found,
            f"model {model!r} {'found' if found else 'not found'} among {len(advertised)} advertised",
        )

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        """An external endpoint has no replicas to plan: the route is registered as is."""
        del spec
        return DeploymentPlan(
            gpu_type=None,
            gpu_count=None,
            replicas_min=0,
            replicas_max=0,
            max_concurrency=None,
            est_vram_gb=None,
            est_cost_per_hour_usd=None,
            operator_instructions_md=None,
        )

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        """Register the existing endpoint and hand it to verification immediately."""
        del plan
        base_url = self._base_url(spec)
        if base_url is None:
            raise EndpointUnavailable(f"model {spec.slug} has no base_url configured")
        model = self._model(spec)
        if model is None:
            raise EndpointUnavailable(f"model {spec.slug} has no upstream model configured")
        credential_ref = self._credential_ref(spec)
        if credential_ref is None:
            raise EndpointUnavailable(f"model {spec.slug} has no api_key_ref configured")
        handle = EndpointHandle(
            endpoint_id=_endpoint_id(spec),
            model=spec,
            state=EndpointState.VERIFYING,
            base_url=base_url,
            upstream_model=model,
            protocol=self._protocol(spec),
            credential_ref=credential_ref,
            external_ids={"account": self.account},
        )
        return handle

    async def _probe(self, handle: EndpointHandle) -> tuple[EndpointState, str, int | None, bool]:
        """Send one ``max_tokens=1`` request and classify the outcome.

        Returns ``(state, detail, http_status, transport_failed)``; ``http_status`` is
        ``None`` when no HTTP response arrived and ``transport_failed`` says whether the
        failure was a connection/timeout problem worth retrying.
        """
        base_url = handle.base_url
        key = self.secrets.resolve(handle.credential_ref)
        model = handle.upstream_model
        if base_url is None or model is None:
            return (
                EndpointState.FAILED,
                "endpoint has no base_url or upstream model",
                None,
                False,
            )
        url = _join(base_url, _HEALTH_PATHS[handle.protocol])
        body = self._health_body(model, handle.protocol)
        try:
            response = await self._post(url, key=key, protocol=handle.protocol, json=body)
        except httpx.HTTPError as exc:
            return EndpointState.FAILED, f"health request failed: {exc}", None, True
        if response.status_code == 429:
            return (
                EndpointState.WARM,
                "upstream is reachable but throttled (HTTP 429)",
                429,
                False,
            )
        if not response.is_success:
            return (
                EndpointState.FAILED,
                f"health request returned HTTP {response.status_code}",
                response.status_code,
                False,
            )
        try:
            payload = response.json()
        except ValueError:
            return EndpointState.FAILED, "health response is not valid JSON", None, False
        reported = payload.get("model") if isinstance(payload, Mapping) else None
        if not isinstance(reported, str) or not reported:
            return EndpointState.FAILED, "health response has no model field", None, False
        if reported != model:
            return (
                EndpointState.FAILED,
                f"model mismatch: expected {model!r}, got {reported!r}",
                None,
                False,
            )
        return EndpointState.READY, f"health request succeeded for {model}", None, False

    @staticmethod
    def _typed_error(status: int, detail: str) -> EndpointMismatch | EndpointUnavailable:
        """Map an HTTP status to the provider error taxonomy used by wake and callers."""
        if status == 429 or status >= 500:
            return EndpointUnavailable(detail, detail={"status": status})
        return EndpointMismatch(detail, detail={"status": status})

    async def status(self, handle: EndpointHandle) -> EndpointState:
        if handle.state is EndpointState.RETIRED:
            return EndpointState.RETIRED
        state, detail, _status, _transport_failed = await self._probe(handle)
        handle.observed["last_health"] = detail
        handle.state = state
        return state

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        """Repeat the health request until success or timeout.

        An unreachable upstream is retried until ``timeout_s`` and then raised as
        ``EndpointUnavailable``. An HTTP status error is mapped immediately: 429 and
        5xx are retryable ``EndpointUnavailable``, other 4xx are terminal
        ``EndpointMismatch``.
        """
        if handle.state is EndpointState.RETIRED:
            return EndpointState.RETIRED
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        attempts = 0
        while True:
            state, detail, status, transport_failed = await self._probe(handle)
            attempts += 1
            handle.observed["wake_attempts"] = attempts
            handle.observed["last_health"] = detail
            if state is EndpointState.READY:
                handle.state = EndpointState.WARM
                return handle.state
            if status is not None:
                raise self._typed_error(status, detail)
            if not transport_failed:
                raise EndpointMismatch(detail)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EndpointUnavailable(detail)
            await asyncio.sleep(min(self._wake_retry_interval_s, remaining))

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        """External endpoints are only deregistered, never shut down."""
        del mode
        handle.state = EndpointState.RETIRED

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base_url")
        default_params = _mapping(self.config.get("default_params"))
        rpm, tpm = self.config.get("rpm"), self.config.get("tpm")
        return UpstreamRoute(
            api_base=handle.base_url,
            model=handle.upstream_model or handle.model.slug,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            default_params=default_params,
            extra_headers=self._extra_headers(),
            provider_lock=self._provider_lock(),
            cold_start_window_s=int(self.config.get("cold_start_window_s", 0)),
            rpm=None if rpm is None else int(rpm),
            tpm=None if tpm is None else int(tpm),
        )
