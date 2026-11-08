"""Application context: build every control-plane component from config + environment.

``AppContext`` wires the stores, secret resolver, provider registry, model
registry, HF checker, gateway admin client, endpoint manager, probe runner,
scheduler, lease manager, planner, agent-runner registry, evaluator and trial
runner behind one async context manager, so each CLI command is a thin layer
over the real components.

Environment overrides:

- ``SPLEEF_STORE_URL`` — result store and (for SQL schemes) event store URL;
  default ``sqlite:///<cwd>/.spleef/store.sqlite3``.
- ``SPLEEF_EVENTS_URL`` — event store URL; default follows the store URL, or
  ``jsonl://<cwd>/.spleef/events`` when the store is not SQL.
- ``SPLEEF_GATEWAY_ADMIN_URL`` / ``SPLEEF_GATEWAY_ADMIN_TOKEN`` — gateway
  admin plane; required by the gateway-dependent commands.
- ``SPLEEF_GATEWAY_URL`` — gateway data plane; overrides
  ``providers.yaml: gateway.internal_url``.
- ``SPLEEF_VAULT_URL`` / ``SPLEEF_VAULT_TOKEN`` — enables ``vault://`` secret
  references.
- ``SPLEEF_STATE_DIR`` — state directory (freeze records); default
  ``<cwd>/.spleef``.

Configuration files may carry ``${VAR}`` environment placeholders; they are
substituted from the process environment before validation and a leftover
placeholder is a usage error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from evalspleef.control.batch import (
    BatchPlan,
    BatchPlanner,
    FreezeRecord,
    ModelPlanInput,
    PlannedTrial,
    TaskPlanInput,
)
from evalspleef.control.config import (
    BatchFileConfig,
    ProvidersFileConfig,
    load_batch,
    load_hf_token_expiry,
    load_models,
    load_providers,
)
from evalspleef.control.endpoints import (
    EndpointManager,
    EndpointNotFound,
    make_endpoint_id,
    public_name,
)
from evalspleef.control.events import EventStore, open_event_store
from evalspleef.control.hf import HuggingFaceChecker
from evalspleef.control.leases import LeaseManager
from evalspleef.control.observe import Evaluator
from evalspleef.control.probes import ProbeRunner, ProbeTarget, report_from_row
from evalspleef.control.probes.clock import SystemClock
from evalspleef.control.providers import ProviderRegistry
from evalspleef.control.registry import ModelRegistry
from evalspleef.control.scheduling import CapacitySpec, Scheduler, StaticMatcher, capacity_spec
from evalspleef.control.secrets import (
    CompositeResolver,
    EnvResolver,
    FileResolver,
    VaultKv2Resolver,
)
from evalspleef.control.store import (
    Database,
    EndpointRepository,
    EndpointTicketRepository,
    GradeRepository,
    HostRepository,
    ModelSpecRepository,
    ProbeReportRepository,
    ProviderInstanceRepository,
    SandboxLeaseRepository,
    TemplateBuildRepository,
    TrialRepository,
    connect,
    migrate,
)
from evalspleef.control.taskpack import TaskPack, load_task_pack
from evalspleef.control.trial import ModelEndpoint, TrialRunner, TrialSetup
from evalspleef.core.batch import BatchState
from evalspleef.core.canary import CanaryRegistry
from evalspleef.core.judge import JudgeClient
from evalspleef.spi.gateway import GatewayAdmin, PriceSpec, TrialKey, UsageRecord
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.model import EndpointState, HFSource, ModelSpec, UpstreamRoute, VendorSource
from evalspleef.spi.probe import ProbeReport
from evalspleef.spi.runner import AgentRunner
from evalspleef.spi.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
    TemplateKind,
    TemplateRef,
)

from .errors import DomainError, UsageError
from .gateway_admin import HttpGatewayAdmin

__all__ = ["AppContext", "Settings"]

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(text: str, environ: Mapping[str, str]) -> str:
    """Substitute ``${VAR}`` placeholders that are defined; leave the rest literal.

    ``config validate`` must accept files whose placeholders are not set yet;
    commands that actually dial the URL check for leftovers themselves
    (:func:`ensure_resolved`).
    """

    def replace(match: re.Match[str]) -> str:
        return environ.get(match.group(1), match.group(0))

    return _ENV_PLACEHOLDER.sub(replace, text)


_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def unresolved_placeholders(value: str) -> list[str]:
    """Names of the ``${VAR}`` placeholders still present in ``value``."""
    return _PLACEHOLDER.findall(value)


def ensure_resolved(value: str | None, what: str) -> None:
    """Usage error naming the unset variables when ``value`` carries placeholders."""
    if value is None:
        return
    missing = unresolved_placeholders(value)
    if missing:
        raise UsageError(
            f"{what} references unset environment placeholder(s) "
            f"{', '.join(f'${{{name}}}' for name in missing)}; export them first"
        )


def _load_interpolated(path: Path, environ: Mapping[str, str]) -> Path:
    """Write the interpolated copy of ``path`` next to the temp files and return it.

    The real loaders in :mod:`evalspleef.control.config` validate the document
    and raise :class:`ConfigError`; interpolating the text beforehand keeps
    that single validation path while allowing environment placeholders.
    """
    text = path.read_text(encoding="utf-8")
    rendered = _interpolate(text, environ)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=path.suffix, prefix=f"{path.stem}.", delete=False
    ) as handle:
        handle.write(rendered)
        tmp = Path(handle.name)
    return tmp


def load_models_config(path: Path, environ: Mapping[str, str]) -> list[ModelSpec]:
    """Load ``models.yaml`` with environment placeholder support."""
    tmp = _load_interpolated(path, environ)
    try:
        return load_models(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def load_hf_token_expiry_config(path: Path, environ: Mapping[str, str]) -> dict[str, date]:
    """HF token expiry dates recorded in ``models.yaml``, by token ref."""
    tmp = _load_interpolated(path, environ)
    try:
        return load_hf_token_expiry(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def load_providers_config(path: Path, environ: Mapping[str, str]) -> ProvidersFileConfig:
    """Load ``providers.yaml`` with environment placeholder support."""
    tmp = _load_interpolated(path, environ)
    try:
        return load_providers(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def load_batch_config(path: Path, environ: Mapping[str, str]) -> BatchFileConfig:
    """Load ``batch.yaml`` with environment placeholder support."""
    tmp = _load_interpolated(path, environ)
    try:
        return load_batch(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def build_secret_resolver(environ: Mapping[str, str]) -> CompositeResolver:
    """env:// and file:// always; vault:// when the Vault address is configured."""
    resolvers: list[Any] = [EnvResolver(environ), FileResolver()]
    vault_url = environ.get("SPLEEF_VAULT_URL")
    vault_token = environ.get("SPLEEF_VAULT_TOKEN")
    if vault_url and vault_token:
        resolvers.append(VaultKv2Resolver(vault_url, vault_token))
    return CompositeResolver(resolvers)


#: Template kinds of the vendor templates the Template Builder records (TECH-DESIGN 7.6).
_VENDOR_TEMPLATE_KINDS: dict[str, TemplateKind] = {
    "novita": "novita_template",
    "e2b": "e2b_template",
}


def price_for_spec(spec: ModelSpec) -> PriceSpec:
    """The frozen price of one route, from the spec's pricing reference.

    Concrete prices may be given in ``deployment.params.pricing``; without
    them the route is costed at zero with the basis the ``pricing_ref`` names
    (``api:`` / ``ref:`` / ``assumed:``).
    """
    raw = spec.pricing_ref
    basis: Literal["api", "ref-price", "assumed"]
    if raw.startswith("ref:"):
        basis = "ref-price"
    elif raw.startswith("assumed:"):
        basis = "assumed"
    else:
        basis = "api"
    table_version = raw.partition("@")[2] if "@" in raw else ""
    params = spec.deployment.params.get("pricing")
    mapping = params if isinstance(params, Mapping) else {}

    def per_mtok(name: str) -> float:
        value = mapping.get(name)
        return float(value) if isinstance(value, int | float) else 0.0

    return PriceSpec(
        input_per_mtok=per_mtok("input_per_mtok"),
        output_per_mtok=per_mtok("output_per_mtok"),
        cached_per_mtok=per_mtok("cached_per_mtok"),
        basis=basis,
        table_version=table_version,
    )


@dataclass(frozen=True)
class Settings:
    """Resolved CLI settings: config paths, store URLs, gateway addresses."""

    models_path: Path
    providers_path: Path | None
    batch_path: Path | None
    tasks_dir: Path
    templates_registry: Path | None
    store_url: str
    events_url: str
    state_dir: Path
    gateway_admin_url: str | None
    gateway_admin_token: str | None
    gateway_url: str | None

    @classmethod
    def from_options(
        cls,
        *,
        models: Path,
        providers: Path | None,
        batch: Path | None,
        tasks_dir: Path,
        templates_registry: Path | None,
        store_url: str | None,
        events_url: str | None,
        state_dir: Path | None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        env = os.environ if environ is None else environ
        resolved_state = state_dir or Path(env.get("SPLEEF_STATE_DIR", ".spleef"))
        resolved_store = store_url or env.get("SPLEEF_STORE_URL") or ""
        if not resolved_store:
            resolved_store = f"sqlite:///{resolved_state / 'store.sqlite3'}"
        resolved_events = events_url or env.get("SPLEEF_EVENTS_URL") or ""
        if not resolved_events:
            if resolved_store.startswith(("sqlite:///", "postgresql://")):
                resolved_events = resolved_store
            else:
                resolved_events = f"jsonl://{resolved_state / 'events'}"
        return cls(
            models_path=models,
            providers_path=providers,
            batch_path=batch,
            tasks_dir=tasks_dir,
            templates_registry=templates_registry,
            store_url=resolved_store,
            events_url=resolved_events,
            state_dir=resolved_state,
            gateway_admin_url=env.get("SPLEEF_GATEWAY_ADMIN_URL"),
            gateway_admin_token=env.get("SPLEEF_GATEWAY_ADMIN_TOKEN"),
            gateway_url=env.get("SPLEEF_GATEWAY_URL"),
        )


class StoredEndpointStates:
    """EndpointStates backed by the endpoint projection rows."""

    def __init__(self, endpoints: EndpointRepository) -> None:
        self._endpoints = endpoints

    async def state(self, endpoint_id: str) -> EndpointState:
        row = await self._endpoints.get(endpoint_id)
        if row is None:
            raise EndpointNotFound(f"no endpoint {endpoint_id!r}")
        return EndpointState(row.state)


class _LazyGatewayAdmin(GatewayAdmin):
    """Defers URL validation until the gateway is actually called.

    Commands like ``models register`` build an EndpointManager without touching
    the gateway; only operations that need the admin plane (probe, warm, trial
    keys) resolve it.
    """

    def __init__(self, context: AppContext) -> None:
        self._context = context

    def _admin(self) -> GatewayAdmin:
        return self._context.gateway_admin()

    async def version(self) -> str:
        return await self._admin().version()

    async def upsert_route(self, name: str, route: UpstreamRoute) -> None:
        await self._admin().upsert_route(name, route)

    async def remove_route(self, name: str) -> None:
        await self._admin().remove_route(name)

    async def set_price(self, name: str, price: PriceSpec) -> None:
        await self._admin().set_price(name, price)

    async def issue_key(
        self,
        *,
        trial_id: str,
        endpoint_id: str,
        max_budget_usd: float,
        max_tokens: int,
        expires_at: str,
    ) -> TrialKey:
        return await self._admin().issue_key(
            trial_id=trial_id,
            endpoint_id=endpoint_id,
            max_budget_usd=max_budget_usd,
            max_tokens=max_tokens,
            expires_at=expires_at,
        )

    async def revoke_key(self, key_id: str) -> None:
        await self._admin().revoke_key(key_id)

    async def usage(self, trial_id: str) -> list[UsageRecord]:
        return await self._admin().usage(trial_id)


_PROSE_PROBE_WAIVER = (
    "waived by the spleef CLI: the trial preflight executes InvariantEntry.probe "
    "verbatim as a shell command, but the adapter ships a prose description there "
    "(cross-component wiring gap; the docker adapter's executable probes/linux.sh "
    "is never invoked). Waived until the control plane runs the executable probe "
    "scripts; the adapter conformance suite still covers these invariants."
)


class _BridgedProvider(SandboxProvider):
    """Delegates to the real provider; ``isolation_profile`` carries prose-probe waivers."""

    def __init__(self, inner: SandboxProvider, profile: IsolationProfile) -> None:
        super().__init__(inner.instance, {}, inner.secrets)
        self.name = inner.name  # the adapter name recorded in trial.versions
        self._inner = inner
        self._profile = profile

    def capabilities(self) -> SandboxCapabilities:
        return self._inner.capabilities()

    def isolation_profile(self) -> IsolationProfile:
        return self._profile

    async def preflight(self) -> dict[str, Any]:
        return await self._inner.preflight()

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await self._inner.create(spec)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        return await self._inner.list(labels)

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        return await self._inner.reap(labels, older_than_s)

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()


def _waive_prose_probes(providers: ProviderRegistry) -> list[str]:
    """Waive non-executable preflight probes (human prose) so trials can preflight.

    Cross-component gap (documented in the control-cli report): adapters fill
    ``InvariantEntry.probe`` with prose descriptions while the trial preflight
    execs the string as a shell command, so the S5 preflight can never pass on a
    real adapter. The waiver is loud, not silent: every preflight event records
    the waived invariants with this reason, and the freeze record carries them.
    Returns the instance names the bridge was applied to.
    """
    bridged: list[str] = []
    for name, instance in providers.sandbox_instances().items():
        profile = instance.isolation_profile()
        invariants: dict[str, InvariantEntry] = {}
        changed = False
        for key, entry in profile.invariants.items():
            if not entry.waived and entry.probe is not None and " " in entry.probe.strip():
                invariants[key] = InvariantEntry(
                    how=entry.how,
                    probe=None,
                    waived=True,
                    reason=_PROSE_PROBE_WAIVER,
                    compensating=entry.compensating,
                    approved_by="spleef-cli",
                    expires=entry.expires,
                )
                changed = True
            else:
                invariants[key] = entry
        if not changed:
            continue
        bridged_profile = IsolationProfile(
            provider=profile.provider,
            declared_isolation=profile.declared_isolation,
            verified_isolation=profile.verified_isolation,
            invariants=invariants,
        )
        providers._sandbox_instances[name] = _BridgedProvider(instance, bridged_profile)
        providers._capabilities_cache.clear()
        providers._isolation_cache.clear()
        bridged.append(name)
    return bridged


class _LazyProbeRunner:
    """Duck-typed lazy stand-in for ProbeRunner (the manager types it structurally)."""

    def __init__(self, context: AppContext) -> None:
        self._context = context

    async def run(self, target: ProbeTarget) -> ProbeReport:
        return await self._context.probe_runner().run(target)

    async def ping(
        self, endpoint_id: str, model_name: str, *, timeout_s: float | None = None
    ) -> Any:
        return await self._context.probe_runner().ping(endpoint_id, model_name, timeout_s=timeout_s)


class AppContext:
    """Every control-plane component, built from a config directory + environment."""

    def __init__(self, settings: Settings, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self._environ = os.environ if environ is None else environ
        self.preflight_warnings: list[str] = []
        self.db: Database
        self.events: EventStore
        self.secrets: CompositeResolver
        self.model_registry: ModelRegistry
        self.hf: HuggingFaceChecker
        self.endpoints: EndpointRepository
        self.tickets: EndpointTicketRepository
        self.probe_reports: ProbeReportRepository
        self.trials: TrialRepository
        self.grades: GradeRepository
        self.lease_rows: SandboxLeaseRepository
        self.hosts: HostRepository
        self.templates: TemplateBuildRepository
        self.provider_instances: ProviderInstanceRepository
        self._gateway_admin: HttpGatewayAdmin | None = None
        self._providers_config: ProvidersFileConfig | None = None
        self._providers: ProviderRegistry | None = None
        self._probe_runner: ProbeRunner | None = None
        self.preflight_bridges: list[str] = []

    async def __aenter__(self) -> AppContext:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = await connect(self.settings.store_url)
        await migrate(self.db)
        self.events = await open_event_store(self.settings.events_url)
        self.secrets = build_secret_resolver(self._environ)
        self.model_registry = ModelRegistry(ModelSpecRepository(self.db))
        self.hf = HuggingFaceChecker(secrets=self.secrets)
        self.endpoints = EndpointRepository(self.db)
        self.tickets = EndpointTicketRepository(self.db)
        self.probe_reports = ProbeReportRepository(self.db)
        self.trials = TrialRepository(self.db)
        self.grades = GradeRepository(self.db)
        self.lease_rows = SandboxLeaseRepository(self.db)
        self.hosts = HostRepository(self.db)
        self.templates = TemplateBuildRepository(self.db)
        self.provider_instances = ProviderInstanceRepository(self.db)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._gateway_admin is not None:
            aclose = getattr(self._gateway_admin, "aclose", None)
            if aclose is not None:
                await aclose()
        if self._providers is not None:
            for provider in self._providers.sandbox_instances().values():
                close = getattr(provider, "close", None)
                if close is not None:
                    await close()
        await self.events.close()
        await self.db.close()

    # -- configuration ---------------------------------------------------------

    def providers_config(self) -> ProvidersFileConfig:
        """The parsed providers.yaml, loaded once per context."""
        if self._providers_config is None:
            if self.settings.providers_path is None:
                raise UsageError("this command needs --providers (or config/providers.yaml)")
            if not self.settings.providers_path.is_file():
                raise UsageError(f"providers config not found: {self.settings.providers_path}")
            self._providers_config = load_providers_config(
                self.settings.providers_path, self._environ
            )
        return self._providers_config

    def provider_registry(self) -> ProviderRegistry:
        """The provider registry; instantiating adapters needs providers.yaml only."""
        if self._providers is None:
            self._providers = ProviderRegistry(self.providers_config(), self.secrets)
            self.preflight_bridges = _waive_prose_probes(self._providers)
        return self._providers

    @property
    def providers(self) -> ProviderRegistry:
        """Alias of :meth:`provider_registry` for template-style access."""
        return self.provider_registry()

    def load_model_specs(self) -> list[ModelSpec]:
        if not self.settings.models_path.is_file():
            raise UsageError(f"models config not found: {self.settings.models_path}")
        return load_models_config(self.settings.models_path, self._environ)

    def load_batch_config(self) -> BatchFileConfig:
        if self.settings.batch_path is None:
            raise UsageError("this command needs --batch (or config/batch.yaml)")
        if not self.settings.batch_path.is_file():
            raise UsageError(f"batch config not found: {self.settings.batch_path}")
        return load_batch_config(self.settings.batch_path, self._environ)

    # -- gateway -----------------------------------------------------------------

    def gateway_admin(self) -> GatewayAdmin:
        """The gateway admin client; a usage error when the admin URL is not set."""
        if self._gateway_admin is None:
            if not self.settings.gateway_admin_url:
                raise UsageError(
                    "SPLEEF_GATEWAY_ADMIN_URL is not set; point it at the gateway admin plane"
                )
            token = self.settings.gateway_admin_token
            if not token:
                raise UsageError("SPLEEF_GATEWAY_ADMIN_TOKEN is not set")
            self._gateway_admin = HttpGatewayAdmin(self.settings.gateway_admin_url, token)
        return self._gateway_admin

    def gateway_data_url(self) -> str:
        """The gateway data-plane root the runners and probes talk to."""
        if self.settings.gateway_url:
            return self.settings.gateway_url
        gateway = self.providers_config().gateway
        if gateway is not None:
            return gateway.internal_url
        raise UsageError(
            "no gateway data plane: set SPLEEF_GATEWAY_URL or providers.yaml: gateway.internal_url"
        )

    # -- managers ----------------------------------------------------------------

    def endpoint_manager(self) -> EndpointManager:
        from typing import cast

        return EndpointManager(
            models=self.model_registry,
            providers=self.providers,
            endpoints=self.endpoints,
            tickets=self.tickets,
            reports=self.probe_reports,
            events=self.events,
            gateway=cast(GatewayAdmin, _LazyGatewayAdmin(self)),
            probes=cast(ProbeRunner, _LazyProbeRunner(self)),
            price_for=price_for_spec,
            hf=self.hf,
            hf_token_expiry=self.hf_token_expiry(),
        )

    def hf_token_expiry(self) -> dict[str, date]:
        """Recorded HF token expiry dates; the batch freeze checks them (TECH-DESIGN 6.5)."""
        if not self.settings.models_path.is_file():
            return {}
        return load_hf_token_expiry_config(self.settings.models_path, self._environ)

    def probe_runner(self) -> ProbeRunner:
        if self._probe_runner is None:
            self._probe_runner = ProbeRunner(
                self.gateway_admin(), gateway_url=self.gateway_data_url()
            )
        return self._probe_runner

    def lease_manager(self) -> LeaseManager:
        return LeaseManager(
            self.providers, self.lease_rows, self.events, templates=self.carrier_template
        )

    def planner(self) -> BatchPlanner:
        return BatchPlanner(StaticMatcher(self.providers))

    async def build_scheduler(self) -> Scheduler:
        """One capacity spec per configured sandbox instance.

        A provider preflight failure (for example a stopped Docker daemon)
        downgrades to declared capabilities only; the warning is recorded on
        ``self.preflight_warnings`` and surfaced by ``providers check``.
        """
        specs: list[CapacitySpec] = []
        for name, instance in self.providers_config().sandbox_instances.items():
            capabilities = self.providers.capabilities(name)
            try:
                preflight = await self.providers.sandbox_instance(name).preflight()
            except Exception as exc:  # the scheduler still works on declared capabilities
                self.preflight_warnings.append(f"{name}: preflight failed: {exc}")
                preflight = {}
            specs.append(capacity_spec(name, capabilities, preflight, instance.limits))
        return Scheduler(specs)

    def runner_classes(self) -> Mapping[str, type[AgentRunner]]:
        """Agent runner classes by shell slug, discovered through entry points."""
        return self.providers.runner_classes()

    async def build_judges(
        self, batch: BatchFileConfig, gateway_url: str
    ) -> Mapping[str, JudgeClient]:
        """Judge panel clients for the batch's judges, keyed by model slug.

        Each judge endpoint gets one batch-scoped gateway key (judges are not
        trial-bound); the key is handed to the judge client and never printed.
        """
        if not batch.judges:
            return {}
        from datetime import UTC, datetime

        from evalspleef.control.observe.judging import GatewayJudgeClient

        # Judge clients speak the OpenAI protocol and post to "/chat/completions"
        # themselves, so their base URL needs the /v1 suffix like the runners'.
        judge_gateway_url = (
            gateway_url
            if gateway_url.rstrip("/").endswith("/v1")
            else f"{gateway_url.rstrip('/')}/v1"
        )
        expires_at = datetime.fromtimestamp(SystemClock().now() + 86400, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        clients: dict[str, JudgeClient] = {}
        for slug in batch.judges:
            try:
                spec = await self.model_registry.resolve(slug)
            except DomainError:
                raise
            except Exception as exc:
                raise DomainError(f"judge model {slug!r} is not registered: {exc}") from exc
            endpoint_id = make_endpoint_id(spec)
            key = await self.gateway_admin().issue_key(
                trial_id=f"judges-{batch.batch}",
                endpoint_id=endpoint_id,
                max_budget_usd=10_000.0,
                max_tokens=50_000_000,
                expires_at=expires_at,
            )
            vendor = (
                spec.source.vendor
                if isinstance(spec.source, VendorSource)
                else spec.deployment.adapter
            )
            clients[slug] = GatewayJudgeClient(
                endpoint_id=endpoint_id.removeprefix("ep_"),  # the client adds the prefix
                gateway_url=judge_gateway_url,
                provider=vendor,
                family=spec.family,
                api_key=key.secret,
                name=public_name(endpoint_id),
            )
        return clients

    async def trial_version_seeds(
        self, plan: BatchPlan, freeze: FreezeRecord
    ) -> dict[str, dict[str, Any]]:
        """Model- and batch-level ``trial.versions`` fields for each endpoint of ``plan``.

        Resolved once when the batch runs, from the registered model spec, the
        endpoint handle, its frozen probe report and the freeze record (PRD
        FR-MOD-09, rule 8 of AGENTS.md: record reproducibility fields at creation).
        """
        manager = self.endpoint_manager()
        seeds: dict[str, dict[str, Any]] = {}
        for cell in plan.runnable_cells:
            endpoint_id = cell.model.endpoint_id
            if endpoint_id is None or endpoint_id in seeds:
                continue
            handle = await manager.get(endpoint_id)
            spec, source = handle.model, handle.model.source
            hf = source if isinstance(source, HFSource) else None
            report_id = freeze.probe_report_ids.get(endpoint_id)
            report = await self.probe_reports.get(report_id) if report_id else None
            effective = report.capabilities.get("context_tokens_effective") if report else None
            seeds[endpoint_id] = {
                "model_version": f"{spec.slug}@{spec.spec_version}"
                + (f"+{hf.revision}" if hf else ""),
                "model_source": "huggingface" if hf else "vendor",
                "hf_repo": hf.repo if hf else None,
                "hf_revision": hf.revision if hf else None,
                "hf_mirror_repo": hf.mirror_repo if hf else None,
                "deployment_adapter": spec.deployment.adapter,
                "deployment_id": handle.observed.get("deployment_id")
                or handle.external_ids.get("novita_endpoint_id")
                or handle.external_ids.get("instance_id"),
                "endpoint_base_url_hash": (
                    "sha256:" + hashlib.sha256(handle.base_url.encode()).hexdigest()
                    if handle.base_url
                    else None
                ),
                "probe_report_id": report_id,
                "context_length_effective": effective if isinstance(effective, int) else None,
                "gateway_version": freeze.gateway_version,
                "freeze_hash": freeze.version_matrix_hash,
            }
        return seeds

    def make_trial_runner(
        self,
        *,
        gateway_url: str,
        transcript_dir: Path | None = None,
        judges: Mapping[str, JudgeClient] | None = None,
        target_family: str = "",
        versions_by_endpoint: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TrialRunner:
        """The real TrialExecutor: packs from the task directory, runners from entry points."""
        # Sandboxes with a read-only rootfs and no /verify mount (the docker
        # adapter) cannot host the default verify root; SPLEEF_VERIFY_ROOT moves
        # verification under a writable tmpfs for those carriers.
        evaluator = Evaluator(
            judges=judges,
            target_family=target_family,
            allow_single_judge=True,  # the example batch runs a one-judge panel
            verify_root=self._environ.get("SPLEEF_VERIFY_ROOT", "/verify"),
        )
        # Agent runners speak the OpenAI protocol: the gateway data-plane URL they
        # get must carry the /v1 suffix (probes take the bare origin instead).
        runner_gateway_url = (
            gateway_url
            if gateway_url.rstrip("/").endswith("/v1")
            else f"{gateway_url.rstrip('/')}/v1"
        )

        def resolve(trial: PlannedTrial) -> TrialSetup:
            if trial.endpoint_id is None:
                raise UsageError(f"planned trial {trial.trial_id} carries no endpoint id")
            return TrialSetup(
                pack=load_task_pack(self.settings.tasks_dir / trial.task_id),
                endpoint=ModelEndpoint(
                    endpoint_id=trial.endpoint_id,
                    model_alias=public_name(trial.endpoint_id),
                    gateway_url=runner_gateway_url,
                ),
                versions=(versions_by_endpoint or {}).get(trial.endpoint_id, {}),
            )

        def runner_factory(trial: PlannedTrial) -> AgentRunner:
            classes = self.runner_classes()
            runner_class = classes.get(trial.shell)
            if runner_class is None:
                raise UsageError(
                    f"no agent runner registered for shell {trial.shell!r}; "
                    f"available: {sorted(classes)}"
                )
            return runner_class()

        kwargs: dict[str, Any] = {}
        if transcript_dir is not None:
            kwargs["scratch_root"] = transcript_dir
        return TrialRunner(
            events=self.events,
            gateway=self.gateway_admin(),
            providers=self.providers,
            resolve=resolve,
            runner_factory=runner_factory,
            evaluator=evaluator,
            canary=CanaryRegistry(),
            clock=SystemClock(),
            **kwargs,
        )

    # -- plan inputs ---------------------------------------------------------------

    async def resolve_endpoint(
        self, spec: ModelSpec, *, batch_id: str | None = None
    ) -> tuple[str, str | None]:
        """Endpoint id and latest probe report id of a registered model."""
        del batch_id  # probes are batch-independent; kept for call-site clarity
        endpoint_id = make_endpoint_id(spec)
        row = await self.endpoints.get(endpoint_id)
        if row is None:
            raise UsageError(
                f"model {spec.slug}@{spec.spec_version} is not registered "
                f"(expected endpoint {endpoint_id}); run 'spleef models register' first"
            )
        report_id = row.observed.get("report_id")
        return endpoint_id, str(report_id) if report_id else None

    async def build_model_inputs(self, batch: BatchFileConfig) -> list[ModelPlanInput]:
        """Batch models resolved to their registered endpoints, probes and shells."""
        from evalspleef.control.registry import ModelNotFoundError

        shells = batch.shells
        inputs: list[ModelPlanInput] = []
        for slug in batch.models:
            try:
                spec = await self.model_registry.resolve(slug)
            except ModelNotFoundError as exc:
                raise DomainError(
                    f"model {slug!r} is not registered; run 'spleef models register' first"
                ) from exc
            shell = shells.native.get(slug, shells.unified)
            if shell is None:
                raise UsageError(
                    f"batch config gives no shell for model {slug!r}: set shells.unified "
                    "or shells.native.<model>"
                )
            endpoint_id, report_id = await self.resolve_endpoint(spec)
            capabilities = None
            if report_id is not None:
                row = await self.probe_reports.get(report_id)
                if row is not None:
                    capabilities = report_from_row(row).capabilities
            hf = spec.source if isinstance(spec.source, HFSource) else None
            inputs.append(
                ModelPlanInput(
                    slug=spec.slug,
                    spec_version=spec.spec_version,
                    shell=shell,
                    endpoint_id=endpoint_id,
                    probe_report_id=report_id,
                    revision=hf.revision if hf is not None else None,
                    self_deployed=hf is not None,
                    capabilities=capabilities,
                )
            )
        return inputs

    def discover_task_packs(self, batch: BatchFileConfig) -> list[TaskPack]:
        """Load every task pack matched by the batch include patterns."""
        import fnmatch

        if not self.settings.tasks_dir.is_dir():
            raise UsageError(f"tasks directory not found: {self.settings.tasks_dir}")
        patterns = batch.tasks.include or ["*"]
        packs: list[TaskPack] = []
        for child in sorted(self.settings.tasks_dir.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if not any(fnmatch.fnmatchcase(child.name, pattern) for pattern in patterns):
                continue
            packs.append(load_task_pack(child))
        if not packs:
            raise UsageError(
                f"no task packs under {self.settings.tasks_dir} match {list(patterns)}"
            )
        return packs

    def template_for(self, pack: TaskPack) -> TemplateRef:
        """The OCI template of one pack from the template registry (digest-pinned)."""
        image = str(pack.metadata.get("resources", {}).get("image", ""))
        registry = self._template_registry()
        entry = registry.get(pack.task_id, {})
        tag = str(entry.get("tag", ""))
        digest = str(entry.get("image_id", ""))
        ref = tag if tag else (f"{image}:local" if image else "")
        if not ref:
            raise UsageError(
                f"no template registered for {pack.task_id}: build it with templates/ "
                "and record it in the template registry"
            )
        return TemplateRef(kind="oci", ref=ref, digest=digest)

    def carrier_template(self, template: TemplateRef, instance: str) -> TemplateRef:
        """The template ``instance`` boots for an OCI ``template`` (TECH-DESIGN 7.6).

        The Template Builder records vendor templates under the registry's ``templates``
        section, keyed by the OCI digest they were built from and the provider instance.
        Without an entry the OCI template is returned and the provider decides whether
        it can boot it (an E2B-family provider rejects it with a kind mismatch).
        """
        built = self._template_registry().get("templates", {}).get(template.digest, {})
        entry = built.get(instance) if isinstance(built, Mapping) else None
        if not isinstance(entry, Mapping) or not entry.get("template_id"):
            return template
        kind = _VENDOR_TEMPLATE_KINDS.get(str(entry.get("vendor", "")))
        if kind is None:
            raise UsageError(
                f"template registry entry for {instance} names unknown vendor {entry.get('vendor')!r}"
            )
        return TemplateRef(kind=kind, ref=str(entry["template_id"]), digest=template.digest)

    def _template_registry(self) -> Mapping[str, Any]:
        path = self.settings.templates_registry
        if path is None:
            path = self.settings.tasks_dir.parent / "templates" / "registry.json"
        if not path.is_file():
            return {}
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def build_task_inputs(self, batch: BatchFileConfig) -> list[TaskPlanInput]:
        tasks: list[TaskPlanInput] = []
        for pack in self.discover_task_packs(batch):
            tasks.append(
                TaskPlanInput(
                    task_id=pack.task_id,
                    version=pack.version,
                    requirements=pack.requirements,
                    template=self.template_for(pack),
                    max_cost_usd=pack.budget.max_cost_usd,
                )
            )
        return tasks

    async def build_plan(self, batch: BatchFileConfig) -> BatchPlan:
        models = await self.build_model_inputs(batch)
        tasks = self.build_task_inputs(batch)
        return self.planner().plan(
            batch.batch,
            models,
            tasks,
            epochs=batch.epochs,
            budget_cap_usd=batch.budget.max_total_usd,
        )

    # -- freeze records ------------------------------------------------------------

    def freeze_path(self, batch_id: str) -> Path:
        return self.settings.state_dir / "freezes" / f"{batch_id}.json"

    def load_stored_freeze(self, batch_id: str) -> Mapping[str, Any] | None:
        path = self.freeze_path(batch_id)
        if not path.is_file():
            return None
        record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return record

    def store_freeze(self, record: Mapping[str, Any]) -> Path:
        path = self.freeze_path(str(record["batch_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    # -- batch report rows ------------------------------------------------------------

    async def batch_state(self, batch_id: str) -> BatchState:
        """Highest batch state implied by the stored trial rows."""
        rows = await self.trials.list_by_batch(batch_id)
        states = [row.state for row in rows]
        if not states:
            return BatchState.DRAFT
        if any(state in ("scored", "invalid", "paused_manual") for state in states):
            return BatchState.GRADED
        return BatchState.RUNNING
