"""``providers.yaml``: provider instances, routes, egress proxies (TECH-DESIGN 14.2)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evalspleef.spi.caps import OSFamily
from evalspleef.spi.sandbox import SandboxRole

from ._yaml import load_yaml_mapping
from .errors import ConfigError

__all__ = [
    "EgressProxyConfig",
    "GatewayConfig",
    "ModelAccountConfig",
    "ProvidersFileConfig",
    "RouteEntry",
    "SandboxInstanceConfig",
    "load_providers",
]


class ModelAccountConfig(BaseModel):
    """A named account on a model adapter; several accounts may share one adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    base_url: str | None = None
    api_key_ref: str | None = None
    params: dict[str, object] = Field(default_factory=dict)

    @field_validator("api_key_ref")
    @classmethod
    def _ref_scheme(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("vault://", "env://", "file://")):
            raise ValueError(f"api_key_ref must be a SecretRef string, got {value!r}")
        return value


class SandboxInstanceConfig(BaseModel):
    """One configured instance of a sandbox adapter (FR-ABS-02: multiple instances per adapter)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    config: dict[str, object] = Field(default_factory=dict)
    limits: dict[str, object] = Field(default_factory=dict)
    egress_proxy: str | None = None


class RouteWhen(BaseModel):
    """Route match condition; at least one key must be present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    os: OSFamily | None = None
    role: SandboxRole | None = None

    @model_validator(mode="after")
    def _some_condition(self) -> RouteWhen:
        if self.os is None and self.role is None:
            raise ValueError("route 'when' must set os and/or role")
        return self

    def matches(self, *, os: OSFamily | None, role: SandboxRole | None) -> bool:
        """True when every condition the entry states equals the request."""
        if self.os is not None and self.os != os:
            return False
        return not (self.role is not None and self.role != role)


class RouteEntry(BaseModel):
    """An ordered route: the first entry whose ``when`` matches wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    when: RouteWhen
    prefer: list[str] = Field(min_length=1)

    @field_validator("prefer")
    @classmethod
    def _prefer_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry or entry.startswith("/") or entry.endswith("/"):
                raise ValueError(
                    f"route preference must be 'instance' or 'instance/adapter': {entry!r}"
                )
        return value


class EgressProxyConfig(BaseModel):
    """An egress allowlist proxy a sandbox may route through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    public_ip: str | None = None


class GatewayConfig(BaseModel):
    """Gateway addresses; the public URL is required for in-sandbox shells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_url: str
    public_url: str | None = None


class ProvidersFileConfig(BaseModel):
    """Top level of ``providers.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_accounts: dict[str, ModelAccountConfig] = Field(default_factory=dict)
    sandbox_instances: dict[str, SandboxInstanceConfig] = Field(default_factory=dict)
    routes: list[RouteEntry] = Field(default_factory=list)
    egress_proxies: dict[str, EgressProxyConfig] = Field(default_factory=dict)
    gateway: GatewayConfig | None = None


def load_providers(path: str | Path) -> ProvidersFileConfig:
    """Parse ``providers.yaml``; raises :class:`ConfigError` naming file and path."""
    file = str(path)
    document = load_yaml_mapping(path)
    try:
        return ProvidersFileConfig.model_validate(document)
    except Exception as exc:
        raise ConfigError.from_validation(file, exc) from exc
