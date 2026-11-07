"""``e2b-selfhost`` provider: self-hosted E2B infrastructure in our own VPC.

Uses the ``e2b`` SDK with ``E2B_DOMAIN`` pointing at the self-hosted cluster
(FR-SBX-05). The SDK surface and semantics are the same envd shape as the Novita
Agent Sandbox, so the whole SPI implementation is shared with
:class:`evalspleef_sbx_e2b_family.base.E2BFamilyProvider`; this class only
supplies SDK entry points, error types and the capability ceiling (driven by our
own hardware instead of vendor quotas).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from e2b import (
    CommandExitException,
    RateLimitException,
    Sandbox,
    SandboxException,
    SandboxNetworkOpts,
    SandboxNetworkUpdate,
    SandboxNotFoundException,
    SandboxQuery,
    SandboxState,
)

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import SandboxCapabilities

from .base import E2BFamilyProvider, SdkSandboxHandle

if TYPE_CHECKING:
    from evalspleef.spi.secrets import SecretResolver

DEFAULT_DOMAIN = "e2b.app"
_TEMPLATE_KIND = "e2b_template"

_EVAL_LEAD = "eval-lead"


def _selfhost_isolation_profile() -> IsolationProfile:
    """Isolation profile for e2b-selfhost (TECH-DESIGN section 7.7).

    E2B runs task workloads in Firecracker microVMs on our own VPC nodes; the
    declared level is L3. ``verified_isolation`` stays ``None`` until a real-host
    run of the conformance C-ISO-* subset fills it in.
    """
    return IsolationProfile(
        provider="e2b-selfhost",
        declared_isolation=Isolation.L3,
        verified_isolation=None,
        invariants={
            "I1": InvariantEntry(
                how="a fresh microVM per trial from an immutable template; "
                "kill after the trial; metadata carries lease/trial labels",
                probe="list(labels)==1 while alive, 0 after destroy",
            ),
            "I2": InvariantEntry(
                how="waived: single-use microVM, the root disk is writable but the "
                "instance is destroyed and never reused",
                waived=True,
                reason="instance-per-trial makes cross-trial disk reuse impossible by "
                "construction; there is no snapshot-restore path into a trial",
                compensating="path hash comparison on key files + canary placement scan "
                "(C-ISO-06) detect any unexpected reuse",
                approved_by=_EVAL_LEAD,
                expires="2027-03-01",
            ),
            "I4": InvariantEntry(
                how="allow_internet_access=False for 'off'; allow_out restricted to the "
                "egress proxy address for allowlists; the VPC blocks sandbox access to "
                "other subnets and 169.254.169.254",
                probe="curl --max-time 3 https://example.com fails in 'off' mode",
            ),
            "I6": InvariantEntry(
                how="task templates are built with set_user(agent) and sudo removed "
                "(template_builder); commands default to the non-root run_as_user",
                probe="id -u != 0 && ! sudo -n true",
            ),
            "I8": InvariantEntry(
                how="every exec runs through 'env -i' with a whitelist, so "
                "platform-injected identifying variables never reach agent processes; "
                "the agent login profile is cleaned in the template",
                probe="env | grep -Ei 'e2b|novita|task_id|trial_id|batch|model|eval' is empty",
            ),
            "I16": InvariantEntry(
                how="allowlist modes route through the shared egress proxy "
                "(via_proxy): only the proxy address is reachable from the sandbox and "
                "URL logs are collected on the proxy",
                probe="proxy log lines >= sandbox request count",
            ),
        },
    )


class E2BSelfHostProvider(E2BFamilyProvider):
    """Sandbox provider backed by self-hosted E2B infrastructure (FR-SBX-05)."""

    name = "e2b-selfhost"

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        super().__init__(instance, config, secrets)
        self._domain = str(config.get("domain") or os.getenv("E2B_DOMAIN") or DEFAULT_DOMAIN)

    # -- configuration ---------------------------------------------------------

    def _template_kind(self) -> str:
        return _TEMPLATE_KIND

    @property
    def domain(self) -> str:
        """E2B_DOMAIN of the self-hosted cluster."""
        return self._domain

    def _client_kwargs(self) -> dict[str, Any]:
        ref = self._api_key_ref()
        api_key = self.secrets.resolve(ref) if ref is not None else None
        return {"api_key": api_key, "domain": self._domain}

    def _capability_defaults(self) -> SandboxCapabilities:
        # Limits are our own hardware, so by default nothing is capped here;
        # providers.yaml supplies them via config["limits"] (see capabilities()).
        return SandboxCapabilities(
            os_families=frozenset({OSFamily.LINUX}),
            arches=frozenset({Arch.X86_64, Arch.ARM64}),
            isolation=Isolation.L3,
            net_modes=frozenset(
                {NetMode.OFF, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR, NetMode.FULL}
            ),
            allowlist_kinds=frozenset({"hostname", "wildcard", "ipv4", "cidr"}),
            dynamic_network_policy=True,
            streaming=True,
            stdin=True,
            snapshot="memory",
            snapshot_fanout=True,
            pause_resume=False,
            gui="none",
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset({"cpu", "memory", "disk"}),
            max_concurrency=None,
            max_lifetime_s=None,
            max_resources=None,
            typical_start_s=10.0,
            reset_cost_s=20.0,
        )

    def _profile(self) -> IsolationProfile:
        return _selfhost_isolation_profile()

    # -- vendor SDK entry points -------------------------------------------------

    def _create_handle(
        self,
        *,
        template: str,
        timeout: int,
        metadata: Mapping[str, str],
        allow_internet_access: bool,
        network: Mapping[str, Any] | None,
    ) -> SdkSandboxHandle:
        return Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=dict(metadata),
            allow_internet_access=allow_internet_access,
            network=_network_opts(network),
            **self._client_kwargs(),
        )

    def _list_handles(
        self, labels: Mapping[str, str]
    ) -> list[tuple[str, Mapping[str, str], float | None]]:
        query = SandboxQuery(metadata=dict(labels), state=[SandboxState.RUNNING])
        paginator = Sandbox.list(query=query, **self._client_kwargs())
        entries: list[tuple[str, Mapping[str, str], float | None]] = []
        while paginator.has_next:
            for info in paginator.next_items():
                entries.append(
                    (
                        str(info.sandbox_id),
                        dict(info.metadata or {}),
                        _epoch(info.started_at),
                    )
                )
        return entries

    def _kill_handle(self, sandbox_id: str) -> bool:
        killed: bool = Sandbox.kill(sandbox_id, **self._client_kwargs())
        return killed

    def _create_snapshot(self, handle: Any, name: str) -> str:
        info = handle.create_snapshot(name=name)
        return str(info.snapshot_id)

    def _set_network(
        self, handle: Any, allow_out: list[str] | None, deny_out: list[str] | None
    ) -> None:
        # e2b replaces the whole egress configuration; omitted fields are cleared.
        update: SandboxNetworkUpdate = {}
        if allow_out is not None:
            update["allow_out"] = allow_out
        if deny_out is not None:
            update["deny_out"] = deny_out
        handle.update_network(update)

    # -- vendor error types --------------------------------------------------------

    def _command_exit_exception(self) -> type[Exception]:
        return CommandExitException

    def _not_found_exceptions(self) -> tuple[type[Exception], ...]:
        return (SandboxNotFoundException,)

    def _rate_limit_exceptions(self) -> tuple[type[Exception], ...]:
        return (RateLimitException,)

    def _vendor_exceptions(self) -> tuple[type[Exception], ...]:
        return (SandboxException,)


def _network_opts(network: Mapping[str, Any] | None) -> SandboxNetworkOpts | None:
    """Build typed e2b create-time network options from the base class mapping."""
    if not network:
        return None
    opts: SandboxNetworkOpts = {}
    if "allow_out" in network:
        opts["allow_out"] = list(network["allow_out"])
    if "deny_out" in network:
        opts["deny_out"] = list(network["deny_out"])
    return opts


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


__all__ = ["DEFAULT_DOMAIN", "E2BSelfHostProvider"]
