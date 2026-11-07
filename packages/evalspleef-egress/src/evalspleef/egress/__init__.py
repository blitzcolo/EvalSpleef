"""Egress proxy: allowlist CONNECT proxy, config generators, URL/DNS audit logs."""

from .configgen import CONFIG_CAVEAT, envoy_yaml, squid_conf
from .logs import AuditLog, DnsRecord, UrlRecord
from .policy import (
    DEFAULT_BLOCKED_CIDRS,
    DEFAULT_BLOCKED_HOSTS,
    REGISTRY_HOSTS,
    Decision,
    MirrorRoute,
    Policy,
    Route,
    load_policy,
)
from .proxy import EgressProxy

__all__ = [
    "CONFIG_CAVEAT",
    "DEFAULT_BLOCKED_CIDRS",
    "DEFAULT_BLOCKED_HOSTS",
    "REGISTRY_HOSTS",
    "AuditLog",
    "Decision",
    "DnsRecord",
    "EgressProxy",
    "MirrorRoute",
    "Policy",
    "Route",
    "UrlRecord",
    "envoy_yaml",
    "load_policy",
    "squid_conf",
]
