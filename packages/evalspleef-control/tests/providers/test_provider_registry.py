"""Tests for the provider registry (entry-point discovery, routing, egress)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from evalspleef.control.config.providers import ProvidersFileConfig
from evalspleef.control.providers import (
    MODEL_PROVIDER_GROUP,
    RUNNER_GROUP,
    SANDBOX_PROVIDER_GROUP,
    ProviderNotFoundError,
    ProviderRegistry,
    RouteError,
)
from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    ValidationReport,
)
from evalspleef.spi.runner import AgentRunner
from evalspleef.spi.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretRef
from evalspleef.testing.secrets import DictSecretResolver

CAPABILITIES = SandboxCapabilities(
    os_families=frozenset({OSFamily.LINUX}),
    arches=frozenset({Arch.X86_64, Arch.ARM64}),
    isolation=Isolation.L2,
    net_modes=frozenset({NetMode.OFF}),
    allowlist_kinds=frozenset(),
    dynamic_network_policy=False,
    streaming=True,
    stdin=True,
    snapshot="none",
    snapshot_fanout=False,
    pause_resume=False,
    gui="none",
    gpu=False,
    port_expose=False,
    host_mounts=False,
    enforces_limits=frozenset({"cpu", "memory"}),
    max_concurrency=80,
    max_lifetime_s=10800,
    max_resources=None,
    typical_start_s=3.0,
    reset_cost_s=10.0,
)

PROFILE = IsolationProfile(provider="fake", declared_isolation=Isolation.L2)


class FakeSandboxAdapter(SandboxProvider):
    """Records constructor arguments; counts capability calls for cache tests."""

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: Any) -> None:
        super().__init__(instance, config, secrets)
        self.capability_calls = 0

    def capabilities(self) -> SandboxCapabilities:
        self.capability_calls += 1
        return CAPABILITIES

    def isolation_profile(self) -> IsolationProfile:
        return PROFILE

    async def preflight(self) -> dict[str, Any]:
        return {"ok": True}

    async def create(self, spec: SandboxSpec) -> Sandbox:
        raise NotImplementedError

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        return []

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        return 0


class FakeModelAdapter(ModelEndpointProvider):
    name = "fake-model"
    management_mode = ManagementMode.EXTERNAL

    def __init__(self, account: str, config: Mapping[str, Any], secrets: Any) -> None:
        super().__init__(account, config, secrets)

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        return ValidationReport(ok=True)

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        raise NotImplementedError

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        raise NotImplementedError

    async def status(self, handle: EndpointHandle) -> EndpointState:
        return EndpointState.READY

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        raise NotImplementedError

    async def teardown(self, handle: EndpointHandle, mode: str) -> None:
        raise NotImplementedError

    def upstream(self, handle: EndpointHandle) -> Any:
        raise NotImplementedError


class FakeRunner(AgentRunner):
    name = "terminus"
    version = "0.1.0"
    supported_modes = frozenset({"remote-tools"})
    required_protocol = "chat_completions"

    async def start(self, sandbox: Sandbox, **kwargs: Any) -> None:
        raise NotImplementedError

    async def send(self, message: str, *, deadline_s: int) -> Any:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint with a canned load()."""

    def __init__(self, name: str, cls: type[Any]) -> None:
        self.name = name
        self._cls = cls

    def load(self) -> type[Any]:
        return self._cls


def fake_entry_points(mapping: dict[str, dict[str, type[Any]]]):
    def source(group: str) -> Iterable[FakeEntryPoint]:
        return [FakeEntryPoint(name, cls) for name, cls in mapping.get(group, {}).items()]

    return source


CONFIG_DICT: dict[str, Any] = {
    "model_accounts": {
        "novita-main": {"adapter": "novita-dedicated", "api_key_ref": "vault://secret/novita/main"},
        "anthropic-official": {
            "adapter": "openai-compatible",
            "base_url": "https://api.anthropic.com",
            "api_key_ref": "vault://secret/anthropic/eval",
        },
    },
    "sandbox_instances": {
        "novita-us-phx-1": {
            "adapter": "novita-sandbox",
            "config": {"region": "us-phx-1", "api_key_ref": "vault://secret/novita/main"},
        },
        "macmini-01": {
            "adapter": "remote",
            "config": {"host": "macmini-01", "providers": ["tart", "docker"]},
        },
        "local-docker": {"adapter": "docker", "config": {"runtime": "runc"}},
    },
    "routes": [
        {
            "when": {"os": "linux"},
            "prefer": ["novita-us-phx-1", "local-docker", "macmini-01/docker"],
        },
        {"when": {"os": "macos"}, "prefer": ["macmini-01/tart"]},
        {"when": {"role": "verifier"}, "prefer": ["local-docker", "novita-us-phx-1"]},
    ],
    "egress_proxies": {
        "cloud": {"url": "http://egress.internal:3128", "public_ip": "203.0.113.10"},
        "macmini-01": {"url": "http://192.168.64.1:3128"},
    },
    "gateway": {"internal_url": "http://gateway.internal:4000"},
}


def make_registry(adapters: dict[str, dict[str, type[Any]]] | None = None) -> ProviderRegistry:
    mapping = adapters or {
        SANDBOX_PROVIDER_GROUP: {
            "novita-sandbox": FakeSandboxAdapter,
            "remote": FakeSandboxAdapter,
            "docker": FakeSandboxAdapter,
        },
        MODEL_PROVIDER_GROUP: {
            "novita-dedicated": FakeModelAdapter,
            "openai-compatible": FakeModelAdapter,
        },
        RUNNER_GROUP: {"terminus": FakeRunner},
    }
    config = ProvidersFileConfig.model_validate(CONFIG_DICT)
    secrets = DictSecretResolver({"vault://secret/novita/main": "n0vita"})
    return ProviderRegistry(config, secrets, entry_point_source=fake_entry_points(mapping))


def test_discovers_all_three_groups() -> None:
    registry = make_registry()
    assert set(registry.sandbox_adapters()) == {"novita-sandbox", "remote", "docker"}
    assert set(registry.model_adapters()) == {"novita-dedicated", "openai-compatible"}
    assert set(registry.runner_classes()) == {"terminus"}


def test_instantiates_instances_per_adapter() -> None:
    registry = make_registry()
    novita = registry.sandbox_instance("novita-us-phx-1")
    assert novita.instance == "novita-us-phx-1"
    assert novita.config["region"] == "us-phx-1"
    # one adapter class, several independent instances
    assert registry.sandbox_instance("local-docker").instance == "local-docker"
    assert registry.sandbox_instance("local-docker") is not novita


def test_model_accounts_instantiated_with_refs() -> None:
    registry = make_registry()
    account = registry.model_provider("anthropic-official")
    assert account.account == "anthropic-official"
    assert account.config["base_url"] == "https://api.anthropic.com"
    assert account.config["api_key_ref"] == "vault://secret/anthropic/eval"
    assert account.secrets.resolve(SecretRef("vault://secret/novita/main")) == "n0vita"


def test_missing_adapter_is_a_named_error() -> None:
    with pytest.raises(ProviderNotFoundError) as excinfo:
        make_registry(adapters={SANDBOX_PROVIDER_GROUP: {"docker": FakeSandboxAdapter}})
    assert "'novita-sandbox'" in str(excinfo.value)
    assert "novita-us-phx-1" in str(excinfo.value)


def test_unknown_instance_lookup_names_options() -> None:
    registry = make_registry()
    with pytest.raises(ProviderNotFoundError) as excinfo:
        registry.sandbox_instance("win-hv-01")
    assert "local-docker" in str(excinfo.value)


def test_capabilities_and_isolation_are_cached() -> None:
    registry = make_registry()
    first = registry.capabilities("novita-us-phx-1")
    second = registry.capabilities("novita-us-phx-1")
    assert first is second
    provider = registry.sandbox_instance("novita-us-phx-1")
    assert isinstance(provider, FakeSandboxAdapter)
    assert provider.capability_calls == 1
    assert registry.isolation_profile("novita-us-phx-1") is PROFILE
    assert registry.isolation_profile("novita-us-phx-1") is PROFILE


def test_route_by_os_first_match_wins() -> None:
    registry = make_registry()
    prefs = registry.route(os="linux")
    assert [(p.instance, p.adapter) for p in prefs] == [
        ("novita-us-phx-1", None),
        ("local-docker", None),
        ("macmini-01", "docker"),
    ]
    assert [(p.instance, p.adapter) for p in registry.route(os="macos")] == [("macmini-01", "tart")]


def test_route_by_role() -> None:
    registry = make_registry()
    prefs = registry.route(role="verifier")
    assert [p.instance for p in prefs] == ["local-docker", "novita-us-phx-1"]
    # os request does not fall through to the role route
    assert registry.route(os="windows") == []


def _docker_only_config(routes: list[dict[str, Any]]) -> ProvidersFileConfig:
    return ProvidersFileConfig.model_validate(
        {
            "sandbox_instances": {
                "local-docker": {"adapter": "docker", "config": {"runtime": "runc"}}
            },
            "routes": routes,
            "egress_proxies": CONFIG_DICT["egress_proxies"],
        }
    )


def test_route_validates_unknown_instance() -> None:
    registry = ProviderRegistry(
        _docker_only_config([{"when": {"os": "linux"}, "prefer": ["ghost-01"]}]),
        DictSecretResolver({}),
        entry_point_source=fake_entry_points(
            {SANDBOX_PROVIDER_GROUP: {"docker": FakeSandboxAdapter}}
        ),
    )
    with pytest.raises(RouteError) as excinfo:
        registry.route(os="linux")
    assert "ghost-01" in str(excinfo.value)


def test_route_rejects_adapter_instance_does_not_host() -> None:
    registry = ProviderRegistry(
        _docker_only_config([{"when": {"os": "linux"}, "prefer": ["local-docker/hyperv"]}]),
        DictSecretResolver({}),
        entry_point_source=fake_entry_points(
            {SANDBOX_PROVIDER_GROUP: {"docker": FakeSandboxAdapter}}
        ),
    )
    with pytest.raises(RouteError) as excinfo:
        registry.route(os="linux")
    assert "local-docker/hyperv" in str(excinfo.value)


def test_route_rejects_unknown_os_and_role() -> None:
    registry = make_registry()
    with pytest.raises(RouteError):
        registry.route(os="fuchsia")
    with pytest.raises(RouteError):
        registry.route(role="supervisor")


def test_egress_proxy_lookup() -> None:
    registry = make_registry()
    by_instance_key = registry.egress_proxy("macmini-01")
    assert by_instance_key is not None
    assert by_instance_key.url == "http://192.168.64.1:3128"
    # instances without a proxy entry get None
    assert registry.egress_proxy("local-docker") is None
    assert registry.egress_proxy("novita-us-phx-1") is None


def test_egress_proxy_explicit_name_wins() -> None:
    registry = ProviderRegistry(
        ProvidersFileConfig.model_validate(
            {
                "sandbox_instances": {
                    "local-docker": {
                        "adapter": "docker",
                        "config": {},
                        "egress_proxy": "cloud",
                    }
                },
                "egress_proxies": CONFIG_DICT["egress_proxies"],
            }
        ),
        DictSecretResolver({}),
        entry_point_source=fake_entry_points(
            {SANDBOX_PROVIDER_GROUP: {"docker": FakeSandboxAdapter}}
        ),
    )
    proxy = registry.egress_proxy("local-docker")
    assert proxy is not None
    assert proxy.public_ip == "203.0.113.10"


def test_egress_proxy_unknown_name_is_named_error() -> None:
    registry = ProviderRegistry(
        ProvidersFileConfig.model_validate(
            {
                "sandbox_instances": {
                    "local-docker": {
                        "adapter": "docker",
                        "config": {},
                        "egress_proxy": "missing-proxy",
                    }
                },
                "egress_proxies": CONFIG_DICT["egress_proxies"],
            }
        ),
        DictSecretResolver({}),
        entry_point_source=fake_entry_points(
            {SANDBOX_PROVIDER_GROUP: {"docker": FakeSandboxAdapter}}
        ),
    )
    with pytest.raises(RouteError) as excinfo:
        registry.egress_proxy("local-docker")
    assert "missing-proxy" in str(excinfo.value)


def test_gateway_section_exposed() -> None:
    registry = make_registry()
    assert registry.gateway is not None
    assert registry.gateway.internal_url == "http://gateway.internal:4000"
