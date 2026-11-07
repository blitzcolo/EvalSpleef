"""Shared fixtures: a provider directory over mock sandbox providers and a requirements factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import pytest

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily, RuntimeRequirements
from evalspleef.spi.sandbox import SandboxCapabilities
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver


@dataclass(frozen=True)
class Pref:
    instance: str


class Directory:
    """``ProviderDirectory`` over mock providers; ``routes`` maps an OS value to instances."""

    def __init__(
        self,
        providers: Mapping[str, MockSandboxProvider],
        routes: Mapping[str, list[str]],
        proxies: frozenset[str] = frozenset(),
    ) -> None:
        self.providers = dict(providers)
        self._routes = routes
        self._proxies = proxies

    def route(self, *, os: OSFamily | str | None = None, role: str | None = None) -> list[Pref]:
        del role
        return [Pref(instance) for instance in self._routes.get(str(os), [])]

    def capabilities(self, instance: str) -> SandboxCapabilities:
        return self.providers[instance].capabilities()

    def egress_proxy(self, instance: str) -> object | None:
        return object() if instance in self._proxies else None


@pytest.fixture
def make_directory() -> Callable[..., Directory]:
    return Directory


@pytest.fixture
def make_provider() -> Callable[..., MockSandboxProvider]:
    def make(instance: str, **capability_changes: Any) -> MockSandboxProvider:
        capabilities = replace(default_capabilities(), **capability_changes)
        return MockSandboxProvider(instance, {}, DictSecretResolver(), capabilities=capabilities)

    return make


@pytest.fixture
def reqs() -> Callable[..., RuntimeRequirements]:
    def make(**changes: Any) -> RuntimeRequirements:
        base = RuntimeRequirements(
            os=OSFamily.LINUX,
            arch=frozenset({Arch.X86_64}),
            min_isolation=Isolation.L1,
            network=NetMode.OFF,
            cpus=1,
            memory_mb=1024,
        )
        return replace(base, **changes)

    return make
