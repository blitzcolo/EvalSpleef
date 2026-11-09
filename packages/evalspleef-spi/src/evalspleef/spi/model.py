"""Model endpoint SPI (TECH-DESIGN section 6.1).

Inference never goes through this interface: all traffic uses the gateway. A provider only
manages where an endpoint comes from and hands the gateway its upstream route.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .errors import CapabilityNotSupported
from .secrets import SecretResolver

ModelRole = Literal["target", "judge", "user"]
WireProtocol = Literal["chat_completions", "responses", "anthropic_messages"]
HFAccess = Literal["public", "gated", "private"]
TeardownMode = Literal["sleep", "terminate"]

#: ``EndpointHandle.external_ids`` key of the provider-side deployment behind an endpoint.
DEPLOYMENT_ID_KEY = "deployment_id"


class ManagementMode(StrEnum):
    API = "api"
    ASSISTED = "assisted"
    EXTERNAL = "external"


@dataclass(frozen=True)
class HFSource:
    repo: str
    revision: str
    """40-hex commit sha, resolved from a branch or tag at registration time."""
    access: HFAccess
    token_ref: str | None = None
    mirror_repo: str | None = None
    adapter_repo: str | None = None
    files_sha256: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VendorSource:
    vendor: str
    model_id: str


@dataclass(frozen=True)
class DeploymentSpec:
    adapter: str
    account: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelSpec:
    """Immutable; any change is a new ``spec_version``."""

    slug: str
    spec_version: int
    family: str
    roles: frozenset[ModelRole]
    source: HFSource | VendorSource
    deployment: DeploymentSpec
    declared_context_tokens: int
    declared_max_output_tokens: int
    pricing_ref: str
    license: str
    owner: str


class EndpointState(StrEnum):
    REGISTERED = "registered"
    REJECTED = "rejected"
    AWAITING_OPERATOR = "awaiting_operator"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    PROBE_FAILED = "probe_failed"
    READY = "ready"
    WARM = "warm"
    SLEEPING = "sleeping"
    DEGRADED = "degraded"
    FAILED = "failed"
    RETIRED = "retired"


@dataclass
class EndpointHandle:
    endpoint_id: str
    """``ep_<slug>_<short hash>``; the model name seen by shells and judges is exactly this."""
    model: ModelSpec
    state: EndpointState
    base_url: str | None
    """Upstream URL. Visible only to the gateway and the endpoint manager."""
    upstream_model: str | None
    protocol: WireProtocol
    credential_ref: str
    external_ids: dict[str, str] = field(default_factory=dict)
    """Provider-side identifiers. Keys are the adapter's own, except the shared convention
    :data:`DEPLOYMENT_ID_KEY`: a provider whose endpoint runs on a deployment, pod, instance
    or endpoint object it can name publishes that id under it, and ``trial.versions``
    records it as ``deployment_id``. Providers without such an object omit the key."""
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    checks: tuple[tuple[str, bool, str], ...] = ()
    """(check_id, passed, detail)"""


@dataclass(frozen=True)
class DeploymentPlan:
    gpu_type: str | None
    gpu_count: int | None
    replicas_min: int
    replicas_max: int
    max_concurrency: int | None
    est_vram_gb: float | None = None
    est_cost_per_hour_usd: float | None = None
    operator_instructions_md: str | None = None
    """Assisted mode: step-by-step console instructions for the operator."""


@dataclass(frozen=True)
class ProviderLock:
    """Aggregator guard: the response field that names the real vendor must match the pattern."""

    response_field: str
    pattern: str


@dataclass(frozen=True)
class UpstreamRoute:
    """What the gateway needs to reach an endpoint. Replaces the free-form dict of the first design."""

    api_base: str
    model: str
    protocol: WireProtocol
    api_key_ref: str
    default_params: Mapping[str, Any] = field(default_factory=dict)
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    provider_lock: ProviderLock | None = None
    cold_start_window_s: int = 0
    """When above zero the gateway retries 503 and connect timeouts for this many seconds (endpoint wake-up)."""
    rpm: int | None = None
    """Requests per minute the gateway admits to this route; never sent upstream."""
    tpm: int | None = None
    """Tokens per minute (projected input plus max output) the gateway admits to this route."""


class ModelEndpointProvider(ABC):
    name: str
    management_mode: ManagementMode

    def __init__(self, account: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        self.account = account
        self.config = config
        self.secrets = secrets

    @abstractmethod
    async def validate(self, spec: ModelSpec) -> ValidationReport:
        """Pre-registration checks (credentials, HF access, quota). Must have no side effects."""

    @abstractmethod
    async def plan(self, spec: ModelSpec) -> DeploymentPlan: ...

    @abstractmethod
    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        """api: create for real; assisted: return an AWAITING_OPERATOR handle; external: VERIFYING."""

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        """Assisted mode only: accept the base_url, model and deployment id the operator pasted back."""
        raise CapabilityNotSupported("attach")

    @abstractmethod
    async def status(self, handle: EndpointHandle) -> EndpointState: ...

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        raise CapabilityNotSupported("scale")

    @abstractmethod
    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        """Send minimal requests until the endpoint is WARM or the timeout expires."""

    @abstractmethod
    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        """api: execute; assisted: open an operator ticket and wait for confirmation; external: mark RETIRED."""

    @abstractmethod
    def upstream(self, handle: EndpointHandle) -> UpstreamRoute: ...
