"""Mock model endpoint providers, one per management mode (TECH-DESIGN section 6.1).

All three share one skeleton and differ in how deploy/attach/scale/teardown move the endpoint
through its lifecycle states:

- ``MockExternalProvider`` (external): registers an existing endpoint and goes to VERIFYING.
- ``MockAssistedProvider`` (assisted): deploy returns an AWAITING_OPERATOR handle carrying
  operator instructions; ``attach`` validates the pasted-back base URL and model and moves to
  VERIFYING; ``wake`` simulates the cold start on the injected clock.
- ``MockApiProvider`` (api): deploy/scale/teardown take effect immediately.

``validate``, ``plan``, ``status`` and ``upstream`` behave like a real provider with no hidden
side effects.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    ProviderLock,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.secrets import SecretResolver

from .clock import Clock, SystemClock

DEFAULT_COLD_START_S = 300.0


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _provider_lock(config: Mapping[str, Any]) -> ProviderLock | None:
    raw = config.get("provider_lock")
    if not isinstance(raw, Mapping):
        return None
    response_field = raw.get("response_field")
    pattern = raw.get("pattern")
    if isinstance(response_field, str) and isinstance(pattern, str):
        return ProviderLock(response_field=response_field, pattern=pattern)
    return None


def _endpoint_id(spec: ModelSpec) -> str:
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


class _MockProvider(ModelEndpointProvider):
    """Shared contract behaviour: configuration checks, plan, status and gateway route."""

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._clock: Clock = clock or SystemClock()

    # -- configuration helpers -------------------------------------------------

    def _base_url(self, spec: ModelSpec) -> str | None:
        value = self.config.get("base_url", spec.deployment.params.get("base_url"))
        return value if isinstance(value, str) and value else None

    def _expected_model(self, spec: ModelSpec) -> str:
        value = self.config.get("model", spec.deployment.params.get("model"))
        if isinstance(value, str) and value:
            return value
        return spec.slug

    def _credential_ref(self, spec: ModelSpec) -> str:
        value = self.config.get("api_key_ref", spec.deployment.params.get("api_key_ref"))
        if isinstance(value, str) and value:
            return value
        return f"vault://models/{spec.slug}"

    def _handle(
        self,
        spec: ModelSpec,
        state: EndpointState,
        base_url: str | None,
        upstream_model: str | None,
    ) -> EndpointHandle:
        return EndpointHandle(
            endpoint_id=_endpoint_id(spec),
            model=spec,
            state=state,
            base_url=base_url,
            upstream_model=upstream_model,
            protocol="chat_completions",
            credential_ref=self._credential_ref(spec),
        )

    # -- SPI -------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        checks: list[tuple[str, bool, str]] = [
            ("slug", bool(spec.slug), f"slug={spec.slug}"),
            ("account", bool(self.account), f"account={self.account}"),
            (
                "context_declared",
                spec.declared_context_tokens > 0,
                f"declared_context_tokens={spec.declared_context_tokens}",
            ),
        ]
        source = spec.source
        if isinstance(source, HFSource):
            try:
                token: str | None = (
                    self.secrets.resolve(source.token_ref) if source.token_ref else None
                )
            except KeyError:
                token = None
            checks.append(("hf_token", source.access == "public" or bool(token), source.access))
        base_url = self._base_url(spec)
        if base_url is not None:
            checks.append(("base_url_https", base_url.startswith("https://"), base_url))
        return ValidationReport(
            ok=all(passed for _check_id, passed, _detail in checks), checks=tuple(checks)
        )

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        params = spec.deployment.params
        instructions = None
        if self.management_mode is ManagementMode.ASSISTED:
            instructions = (
                f"## Deploy {spec.slug}\n"
                f"1. Open the vendor console for account `{self.account}`.\n"
                f"2. Create a deployment named `ep_{spec.slug}` from `{spec.source}`.\n"
                f"3. Set min/max replicas to {params.get('min_replicas', 1)}."
            )
        return DeploymentPlan(
            gpu_type=params.get("gpu_type"),
            gpu_count=params.get("gpu_count"),
            replicas_min=int(params.get("min_replicas", 1)),
            replicas_max=int(params.get("max_replicas", 1)),
            max_concurrency=params.get("max_concurrency"),
            est_vram_gb=params.get("est_vram_gb"),
            est_cost_per_hour_usd=params.get("est_cost_per_hour_usd"),
            operator_instructions_md=instructions,
        )

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        if self.management_mode is ManagementMode.ASSISTED:
            handle = self._handle(spec, EndpointState.AWAITING_OPERATOR, None, None)
            handle.observed["operator_instructions_md"] = plan.operator_instructions_md or ""
            return handle
        state = (
            EndpointState.VERIFYING
            if self.management_mode is ManagementMode.EXTERNAL
            else EndpointState.READY
        )
        return self._handle(spec, state, self._base_url(spec), self._expected_model(spec))

    async def status(self, handle: EndpointHandle) -> EndpointState:
        return handle.state

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        """Simulate a cold start; the elapsed time comes from the injected clock."""
        if handle.state is EndpointState.WARM:
            return handle.state
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base_url yet")
        cold_start_s = float(self.config.get("cold_start_s", DEFAULT_COLD_START_S))
        handle.state = EndpointState.SLEEPING
        if cold_start_s > timeout_s:
            return handle.state
        if cold_start_s > 0:
            await self._clock.sleep(cold_start_s)
        handle.observed["cold_start_s"] = cold_start_s
        handle.state = EndpointState.WARM
        return handle.state

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        handle.state = EndpointState.SLEEPING if mode == "sleep" else EndpointState.RETIRED

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base_url yet")
        return UpstreamRoute(
            api_base=handle.base_url,
            model=handle.upstream_model or handle.model.slug,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            default_params=_mapping(self.config.get("default_params")),
            extra_headers={
                str(key): str(value)
                for key, value in _mapping(self.config.get("extra_headers")).items()
            },
            provider_lock=_provider_lock(self.config),
            cold_start_window_s=int(self.config.get("cold_start_window_s", 0)),
        )


class MockExternalProvider(_MockProvider):
    """External mode: the endpoint already exists; the provider only registers it."""

    name = "mock-external"
    management_mode = ManagementMode.EXTERNAL

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        del timeout_s  # an existing endpoint is already warm
        handle.state = EndpointState.WARM
        return handle.state

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        del mode  # external endpoints are merely deregistered, never shut down
        handle.state = EndpointState.RETIRED


class MockAssistedProvider(_MockProvider):
    """Assisted mode: an operator deploys by hand; the provider validates the pasted result."""

    name = "mock-assisted"
    management_mode = ManagementMode.ASSISTED

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        handle = self._handle(spec, EndpointState.AWAITING_OPERATOR, None, None)
        handle.observed["operator_instructions_md"] = plan.operator_instructions_md or ""
        return handle

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        base_url = operator_input.get("base_url", "")
        model = operator_input.get("model", "")
        if not base_url.startswith("https://"):
            raise EndpointMismatch(
                "assisted attach requires an https base_url",
                detail={"base_url": base_url, "reason": "base_url_not_https"},
            )
        expected = self._expected_model(handle.model)
        if not model or model != expected:
            raise EndpointMismatch(
                "attached model name does not match the registered model",
                detail={"expected": expected, "actual": model, "reason": "model_mismatch"},
            )
        handle.base_url = base_url
        handle.upstream_model = model
        if operator_input.get("deployment_id"):
            handle.observed["deployment_id"] = operator_input["deployment_id"]
        handle.state = EndpointState.VERIFYING
        return handle

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        # No management API: record the operator ticket and leave the state untouched.
        handle.observed["pending_scale"] = {
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
        }

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        handle.observed["pending_teardown"] = mode


class MockApiProvider(_MockProvider):
    """API mode: every lifecycle operation applies immediately."""

    name = "mock-api"
    management_mode = ManagementMode.API

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        handle.observed["min_replicas"] = min_replicas
        handle.observed["max_replicas"] = max_replicas
        handle.state = EndpointState.SLEEPING if max_replicas == 0 else EndpointState.WARM
