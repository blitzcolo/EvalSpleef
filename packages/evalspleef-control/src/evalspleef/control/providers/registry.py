"""Provider registry: entry-point adapter discovery and instance management."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, get_args

from evalspleef.control.config.providers import (
    EgressProxyConfig,
    GatewayConfig,
    ProvidersFileConfig,
    SandboxInstanceConfig,
)
from evalspleef.spi.caps import OSFamily
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.model import ModelEndpointProvider
from evalspleef.spi.runner import AgentRunner
from evalspleef.spi.sandbox import SandboxCapabilities, SandboxProvider, SandboxRole
from evalspleef.spi.secrets import SecretResolver

__all__ = [
    "MODEL_PROVIDER_GROUP",
    "RUNNER_GROUP",
    "SANDBOX_PROVIDER_GROUP",
    "ProviderNotFoundError",
    "ProviderRegistry",
    "RouteError",
    "RoutePreference",
]

#: Entry-point groups adapters register in (TECH-DESIGN section 4).
SANDBOX_PROVIDER_GROUP = "evalspleef.sandbox_providers"
MODEL_PROVIDER_GROUP = "evalspleef.model_providers"
RUNNER_GROUP = "evalspleef.runners"

#: Callable returning the entry points of one group; injectable for tests.
EntryPointSource = Callable[[str], Iterable[EntryPoint]]


def _default_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)


class ProviderNotFoundError(SpleefError):
    """An adapter named by the configuration is not installed."""

    error_class = "version"
    disposition = "manual"


class RouteError(SpleefError):
    """A ``routes`` entry references an unknown instance or adapter."""

    error_class = "version"
    disposition = "manual"


@dataclass(frozen=True)
class RoutePreference:
    """One entry of a matched route's ``prefer`` list.

    ``adapter`` is the sub-adapter of a hostd-backed instance (for example
    ``docker`` in ``macmini-01/docker``); it is None for plain instance
    references.
    """

    instance: str
    adapter: str | None


class ProviderRegistry:
    """Discovers adapters through entry points and instantiates configured instances.

    ``entry_points`` is injectable so tests can register fake adapters without
    installing distributions; by default the real
    :func:`importlib.metadata.entry_points` is used. Adapter modules are only
    ever imported through their entry point's ``load()`` — never statically.
    """

    def __init__(
        self,
        config: ProvidersFileConfig,
        secrets: SecretResolver,
        *,
        entry_point_source: EntryPointSource = _default_entry_points,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._sandbox_adapters = self._load_adapters(entry_point_source, SANDBOX_PROVIDER_GROUP)
        self._model_adapters = self._load_adapters(entry_point_source, MODEL_PROVIDER_GROUP)
        self._runner_adapters = self._load_adapters(entry_point_source, RUNNER_GROUP)

        self._sandbox_instances: dict[str, SandboxProvider] = {
            name: self._instantiate_sandbox(name, instance)
            for name, instance in config.sandbox_instances.items()
        }
        self._model_providers: dict[str, ModelEndpointProvider] = {
            name: self._instantiate_model_account(name) for name in config.model_accounts
        }
        self._capabilities_cache: dict[str, SandboxCapabilities] = {}
        self._isolation_cache: dict[str, IsolationProfile] = {}

    # -- discovery -----------------------------------------------------------

    @staticmethod
    def _load_adapters(source: EntryPointSource, group: str) -> dict[str, type[Any]]:
        adapters: dict[str, type[Any]] = {}
        for ep in source(group):
            adapters[ep.name] = ep.load()
        return adapters

    def _instantiate_sandbox(self, name: str, cfg: SandboxInstanceConfig) -> SandboxProvider:
        adapter_cls = self._sandbox_adapters.get(cfg.adapter)
        if adapter_cls is None:
            raise ProviderNotFoundError(
                f"sandbox adapter {cfg.adapter!r} required by instance {name!r} is not "
                f"installed (entry-point group {SANDBOX_PROVIDER_GROUP!r}); "
                f"available: {sorted(self._sandbox_adapters)}"
            )
        provider = adapter_cls(instance=name, config=dict(cfg.config), secrets=self._secrets)
        return provider  # type: ignore[no-any-return]

    def _instantiate_model_account(self, name: str) -> ModelEndpointProvider:
        account = self._config.model_accounts[name]
        adapter_cls = self._model_adapters.get(account.adapter)
        if adapter_cls is None:
            raise ProviderNotFoundError(
                f"model adapter {account.adapter!r} required by account {name!r} is not "
                f"installed (entry-point group {MODEL_PROVIDER_GROUP!r}); "
                f"available: {sorted(self._model_adapters)}"
            )
        config: dict[str, Any] = dict(account.params)
        if account.base_url is not None:
            config["base_url"] = account.base_url
        if account.api_key_ref is not None:
            config["api_key_ref"] = account.api_key_ref
        provider = adapter_cls(account=name, config=config, secrets=self._secrets)
        return provider  # type: ignore[no-any-return]

    # -- adapters ------------------------------------------------------------

    def sandbox_adapters(self) -> dict[str, type[Any]]:
        """Discovered sandbox adapter classes by entry-point name."""
        return dict(self._sandbox_adapters)

    def model_adapters(self) -> dict[str, type[Any]]:
        """Discovered model adapter classes by entry-point name."""
        return dict(self._model_adapters)

    def runner_classes(self) -> dict[str, type[AgentRunner]]:
        """Discovered agent runner classes by shell slug."""
        return dict(self._runner_adapters)

    # -- instances -----------------------------------------------------------

    def sandbox_instance(self, name: str) -> SandboxProvider:
        try:
            return self._sandbox_instances[name]
        except KeyError:
            raise ProviderNotFoundError(
                f"no sandbox instance {name!r}; configured: {sorted(self._sandbox_instances)}"
            ) from None

    def sandbox_instances(self) -> dict[str, SandboxProvider]:
        return dict(self._sandbox_instances)

    def model_provider(self, account: str) -> ModelEndpointProvider:
        try:
            return self._model_providers[account]
        except KeyError:
            raise ProviderNotFoundError(
                f"no model account {account!r}; configured: {sorted(self._model_providers)}"
            ) from None

    def model_accounts(self) -> dict[str, ModelEndpointProvider]:
        return dict(self._model_providers)

    def instance_config(self, name: str) -> SandboxInstanceConfig:
        try:
            return self._config.sandbox_instances[name]
        except KeyError:
            raise ProviderNotFoundError(
                f"no sandbox instance {name!r}; configured: {sorted(self._config.sandbox_instances)}"
            ) from None

    # -- cached declared values ----------------------------------------------

    def capabilities(self, instance: str) -> SandboxCapabilities:
        """Declared capabilities of an instance, cached after the first read."""
        if instance not in self._capabilities_cache:
            self._capabilities_cache[instance] = self.sandbox_instance(instance).capabilities()
        return self._capabilities_cache[instance]

    def isolation_profile(self, instance: str) -> IsolationProfile:
        """The adapter's shipped isolation profile, cached after the first read."""
        if instance not in self._isolation_cache:
            self._isolation_cache[instance] = self.sandbox_instance(instance).isolation_profile()
        return self._isolation_cache[instance]

    # -- routing ---------------------------------------------------------------

    def _parse_preference(self, entry: str) -> RoutePreference:
        instance, sep, adapter = entry.partition("/")
        if not instance or (sep and not adapter):
            raise RouteError(f"malformed route preference {entry!r}")
        if instance not in self._config.sandbox_instances:
            raise RouteError(
                f"route preference {entry!r} references unknown instance {instance!r}; "
                f"configured: {sorted(self._config.sandbox_instances)}"
            )
        if sep:
            cfg = self._config.sandbox_instances[instance]
            hosted = cfg.config.get("providers")
            hosted_list = list(hosted) if isinstance(hosted, list) else []
            if adapter != cfg.adapter and adapter not in hosted_list:
                raise RouteError(
                    f"route preference {entry!r}: instance {instance!r} neither is nor "
                    f"hosts adapter {adapter!r}"
                )
        return RoutePreference(instance=instance, adapter=adapter if sep else None)

    def route(
        self,
        *,
        os: OSFamily | str | None = None,
        role: str | None = None,
    ) -> list[RoutePreference]:
        """Resolve the first matching ``routes`` entry to validated preferences.

        Entries are tried in file order (TECH-DESIGN section 7.2); a ``when``
        block matches when every condition it states equals the request. An
        empty result means no route applies.
        """
        os_family = self._coerce_os(os)
        sandbox_role = self._coerce_role(role)
        for entry in self._config.routes:
            if not entry.when.matches(os=os_family, role=sandbox_role):
                continue
            return [self._parse_preference(item) for item in entry.prefer]
        return []

    @staticmethod
    def _coerce_os(value: OSFamily | str | None) -> OSFamily | None:
        if value is None or isinstance(value, OSFamily):
            return value
        try:
            return OSFamily(value)
        except ValueError:
            raise RouteError(f"unknown OS family in route request: {value!r}") from None

    @staticmethod
    def _coerce_role(value: str | None) -> SandboxRole | None:
        if value is None:
            return None
        if value in get_args(SandboxRole):
            return value  # type: ignore[return-value]
        raise RouteError(f"unknown sandbox role in route request: {value!r}")

    # -- egress ----------------------------------------------------------------

    def egress_proxy(self, instance: str) -> EgressProxyConfig | None:
        """The egress proxy a provider instance must route through, if any.

        An explicit ``egress_proxy`` name on the instance wins; otherwise an
        ``egress_proxies`` entry keyed by the instance name applies.
        """
        cfg = self.instance_config(instance)
        proxy_name = cfg.egress_proxy
        if proxy_name is None and instance in self._config.egress_proxies:
            proxy_name = instance
        if proxy_name is None:
            return None
        try:
            return self._config.egress_proxies[proxy_name]
        except KeyError:
            raise RouteError(
                f"instance {instance!r} names egress proxy {proxy_name!r} which is not "
                f"configured: {sorted(self._config.egress_proxies)}"
            ) from None

    @property
    def gateway(self) -> GatewayConfig | None:
        """The ``gateway`` section of providers.yaml, when present."""
        return self._config.gateway
