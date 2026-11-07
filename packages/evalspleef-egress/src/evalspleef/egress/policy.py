"""Network policy for the egress proxy (PRD appendix D.3).

The policy is a frozen value object loaded from YAML. It answers one question for the proxy:
may this request leave the sandbox, and if so where should it be sent. Modes and spellings
follow ``evalspleef.spi.NetMode`` and ``task.yaml`` verbatim.

Policy YAML shape::

    mode: package_mirror            # off | docs_whitelist | package_mirror | full
    allow:                          # hostnames, wildcards, optional path prefixes
      - developer.huawei.com
      - "*.example.com"
      - https://docs.example.com/guide
    block:                          # extra global blocklist entries
      - leaderboard.example.net
    blocked_cidrs:
      - 169.254.169.254/32
    mirrors:                        # registry name -> internal read-only mirror base URL
      pip: http://pypi-mirror.internal:8080
      npm: http://npm-mirror.internal:4873

The platform blocklist and the metadata CIDRs are always added when the policy is loaded from
YAML (PRD D.3: the global blocklist applies in any mode).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import yaml

from evalspleef.spi import NetMode

DEFAULT_BLOCKED_HOSTS: tuple[str, ...] = (
    "git.internal",
    "leaderboard.internal",
    "eval-artifacts.internal",
)
"""Platform-owned repositories, leaderboards and artifact stores (PRD D.3)."""

DEFAULT_BLOCKED_CIDRS: tuple[str, ...] = ("169.254.169.254/32", "169.254.0.0/16")
"""Cloud metadata endpoints and the link-local range they live in."""

READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

REGISTRY_HOSTS: Mapping[str, tuple[str, ...]] = {
    "pip": ("pypi.org", "files.pythonhosted.org"),
    "npm": ("registry.npmjs.org",),
    "cargo": ("crates.io", "static.crates.io", "index.crates.io"),
    "ohpm": ("ohpm.openharmony.cn",),
}
"""Upstream hosts whose paths are rewritten to the configured read-only mirror."""

BLOCK_MODE_OFF = "mode_off"
BLOCK_BLOCKED_HOST = "blocked_host"
BLOCK_BLOCKED_CIDR = "blocked_cidr"
BLOCK_METHOD = "method_not_allowed"
BLOCK_HOST_NOT_ALLOWED = "host_not_allowed"
BLOCK_MIRROR_CONNECT = "mirror_connect_not_supported"
BLOCK_INVALID_HOST = "invalid_host"


def normalize_host(host: str) -> str:
    """Lowercase a host literal, strip brackets and a trailing root dot, canonicalize IPs."""
    value = host.strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def _host_matches(host: str, pattern: str) -> bool:
    """Match a normalized host against an exact name or a ``*.suffix`` wildcard."""
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


def _split_entry(entry: str) -> tuple[str, str]:
    """Split an allowlist entry into ``(host_pattern, path_prefix)``.

    Entries may be bare hosts, wildcards, or URLs. The port and query are ignored; a URL path
    becomes a prefix that plain-HTTP requests must satisfy.
    """
    raw = entry.strip()
    if not raw:
        raise ValueError("empty allowlist entry")
    if any(char.isspace() for char in raw):
        raise ValueError(f"invalid allowlist entry {entry!r}")
    try:
        return str(ipaddress.ip_address(raw.strip("[]"))), ""
    except ValueError:
        pass
    candidate = raw if "://" in raw else f"//{raw}"
    split = urlsplit(candidate)
    if split.username or split.password:
        raise ValueError(f"credentials are not allowed in policy entries: {entry!r}")
    try:
        host = split.hostname
    except ValueError as exc:
        raise ValueError(f"invalid allowlist entry {entry!r}") from exc
    if not host:
        raise ValueError(f"invalid allowlist entry {entry!r}")
    path = split.path.rstrip("/")
    return normalize_host(host) if not host.startswith("*.") else host.lower(), path


def _path_matches(path: str, prefix: str) -> bool:
    if not prefix:
        return True
    return path == prefix or path.startswith(f"{prefix}/") or path.startswith(f"{prefix}?")


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, list | tuple):
        raise ValueError(f"policy.{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"policy.{field_name} must contain only strings")
        items.append(item)
    return tuple(items)


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class MirrorRoute:
    """One registry mapped to an internal read-only mirror."""

    registry: str
    hosts: tuple[str, ...]
    base_url: str
    base_host: str = field(init=False)
    base_port: int = field(init=False)
    base_path: str = field(init=False)
    base_tls: bool = field(init=False)

    def __post_init__(self) -> None:
        split = urlsplit(self.base_url)
        if split.scheme not in ("http", "https") or not split.hostname:
            raise ValueError(f"mirror base URL must be http(s) with a host: {self.base_url!r}")
        tls = split.scheme == "https"
        port = split.port or (443 if tls else 80)
        object.__setattr__(self, "base_host", normalize_host(split.hostname))
        object.__setattr__(self, "base_port", port)
        object.__setattr__(self, "base_path", split.path.rstrip("/"))
        object.__setattr__(self, "base_tls", tls)

    def rewrite_path(self, path: str) -> str:
        """Prefix the request path with the mirror base path, preserving the query."""
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{self.base_path}{suffix}" or "/"


@dataclass(frozen=True)
class Route:
    """Where an allowed request is actually sent."""

    host: str
    port: int
    path: str
    tls: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    """``blocked_reason`` for the audit log when ``allowed`` is false."""
    route: Route | None = None


def _allow(route: Route) -> Decision:
    return Decision(True, None, route)


def _deny(reason: str) -> Decision:
    return Decision(False, reason, None)


@dataclass(frozen=True)
class Policy:
    """Allowlist, blocklist, blocked CIDRs and package-mirror routes."""

    mode: NetMode
    allow: tuple[str, ...] = ()
    block: tuple[str, ...] = DEFAULT_BLOCKED_HOSTS
    blocked_cidrs: tuple[str, ...] = DEFAULT_BLOCKED_CIDRS
    mirrors: tuple[MirrorRoute, ...] = ()
    _networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        init=False, repr=False, compare=False
    )
    _allow_rules: tuple[tuple[str, str], ...] = field(init=False, repr=False, compare=False)
    _block_rules: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", NetMode(self.mode))
        allow = _dedupe(tuple(entry.strip() for entry in self.allow))
        block = _dedupe(tuple(entry.strip() for entry in self.block))
        object.__setattr__(self, "allow", allow)
        object.__setattr__(self, "block", block)

        allow_rules: list[tuple[str, str]] = []
        for entry in allow:
            allow_rules.append(_split_entry(entry))
        block_rules: list[str] = []
        for entry in block:
            host, _ = _split_entry(entry)
            block_rules.append(host)

        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for cidr in self.blocked_cidrs:
            try:
                networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError as exc:
                raise ValueError(f"invalid blocked CIDR {cidr!r}") from exc

        object.__setattr__(self, "_allow_rules", tuple(allow_rules))
        object.__setattr__(self, "_block_rules", tuple(block_rules))
        object.__setattr__(self, "_networks", tuple(networks))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Policy:
        """Validate a decoded YAML mapping and build a policy.

        Unknown keys are rejected instead of silently ignored: a typo in a network policy must
        fail closed at load time, not open the sandbox.
        """
        known = {"mode", "allow", "block", "blocked_cidrs", "mirrors"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown policy keys: {', '.join(sorted(unknown))}")

        raw_mode = data.get("mode", "off")
        if not isinstance(raw_mode, str):
            raise ValueError("policy.mode must be a string")
        try:
            mode = NetMode(raw_mode)
        except ValueError as exc:
            valid = ", ".join(item.value for item in NetMode)
            raise ValueError(f"invalid policy.mode {raw_mode!r}; expected one of {valid}") from exc

        allow = _string_tuple(data.get("allow", []), "allow")
        raw_block = data.get("block")
        extra_block = _string_tuple(raw_block, "block") if raw_block is not None else ()
        raw_cidrs = data.get("blocked_cidrs")
        extra_cidrs = _string_tuple(raw_cidrs, "blocked_cidrs") if raw_cidrs is not None else ()

        raw_mirrors = data.get("mirrors", {})
        if not isinstance(raw_mirrors, Mapping):
            raise ValueError("policy.mirrors must be a mapping of registry name to base URL")
        mirrors: list[MirrorRoute] = []
        for name, base_url in raw_mirrors.items():
            if name not in REGISTRY_HOSTS:
                valid = ", ".join(sorted(REGISTRY_HOSTS))
                raise ValueError(f"unsupported mirror registry {name!r}; expected one of {valid}")
            if not isinstance(base_url, str):
                raise ValueError(f"policy.mirrors.{name} must be a URL string")
            mirrors.append(MirrorRoute(str(name), REGISTRY_HOSTS[name], base_url))

        return cls(
            mode=mode,
            allow=allow,
            block=_dedupe((*DEFAULT_BLOCKED_HOSTS, *extra_block)),
            blocked_cidrs=_dedupe((*DEFAULT_BLOCKED_CIDRS, *extra_cidrs)),
            mirrors=tuple(mirrors),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ValueError(f"policy file {path!s} must contain a mapping")
        return cls.from_mapping(cast(Mapping[str, Any], data))

    @property
    def allow_rules(self) -> tuple[tuple[str, str], ...]:
        """``(host_pattern, path_prefix)`` pairs derived from ``allow``."""
        return self._allow_rules

    @property
    def block_rules(self) -> tuple[str, ...]:
        """Host patterns derived from ``block``."""
        return self._block_rules

    @property
    def blocked_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """Parsed form of ``blocked_cidrs``."""
        return self._networks

    def mirror_for(self, host: str) -> MirrorRoute | None:
        for route in self.mirrors:
            if any(_host_matches(host, upstream) for upstream in route.hosts):
                return route
        return None

    def is_blocked_address(self, host: str) -> bool:
        """True when ``host`` is an IP literal inside one of the blocked CIDRs."""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(
            address.version == network.version and address in network for network in self._networks
        )

    def _host_blocked(self, host: str) -> bool:
        return any(_host_matches(host, pattern) for pattern in self._block_rules)

    def _host_allowed(self, host: str, path: str, *, is_connect: bool) -> bool:
        for pattern, prefix in self._allow_rules:
            if not _host_matches(host, pattern):
                continue
            # A CONNECT tunnel exposes no path, so path-scoped entries can only be checked
            # for plain HTTP. This limitation is documented in the README and config caveat.
            if is_connect or _path_matches(path, prefix):
                return True
        return False

    def decide(
        self,
        *,
        method: str,
        host: str,
        port: int,
        path: str = "/",
        is_connect: bool = False,
    ) -> Decision:
        """Decide one request. Blocked CIDRs and the global blocklist win over any allow rule."""
        normalized_host = normalize_host(host)
        normalized_method = method.upper()
        if not normalized_host or "/" in normalized_host:
            return _deny(BLOCK_INVALID_HOST)
        if self.mode is NetMode.OFF:
            return _deny(BLOCK_MODE_OFF)
        if self.is_blocked_address(normalized_host):
            return _deny(BLOCK_BLOCKED_CIDR)
        if self._host_blocked(normalized_host):
            return _deny(BLOCK_BLOCKED_HOST)

        read_only_mode = self.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR)
        if (
            read_only_mode
            and normalized_method not in READ_ONLY_METHODS
            and not (is_connect and normalized_method == "CONNECT")
        ):
            return _deny(BLOCK_METHOD)

        if self.mode is NetMode.PACKAGE_MIRROR:
            mirror = self.mirror_for(normalized_host)
            if mirror is not None:
                # HTTPS registry traffic cannot be path-rewritten without TLS interception.
                # Deny it explicitly instead of silently allowing an unlogged, unwritable tunnel.
                if is_connect:
                    return _deny(BLOCK_MIRROR_CONNECT)
                return _allow(
                    Route(
                        host=mirror.base_host,
                        port=mirror.base_port,
                        path=mirror.rewrite_path(path),
                        tls=mirror.base_tls,
                    )
                )
            if any(mirror.base_host == normalized_host for mirror in self.mirrors):
                return _allow(Route(normalized_host, port, path))

        if self.mode is NetMode.FULL:
            return _allow(Route(normalized_host, port, path))

        if self._host_allowed(normalized_host, path, is_connect=is_connect):
            return _allow(Route(normalized_host, port, path))
        return _deny(BLOCK_HOST_NOT_ALLOWED)


def load_policy(path: str | Path) -> Policy:
    """Load a policy YAML file."""
    return Policy.from_yaml(path)
