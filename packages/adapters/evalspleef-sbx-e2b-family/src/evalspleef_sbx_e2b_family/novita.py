# mypy: disable-error-code=import-untyped
"""``novita-sandbox`` provider: Novita AI Agent Sandbox (SDK ``novita-sandbox`` 2.x).

Region defaults to ``us-phx-1`` (the v2 region with Secrets and Snapshots support,
FR-SBX-04). The Novita API key is a control-plane-only credential: it is resolved
through the SPI secret resolver at the moment of use and is never passed to the
sandbox (not as a create option, not as metadata, not as an environment variable),
per FR-ISO-02.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from novita_sandbox import (
    CommandExitException,
    Novita,
    RateLimitException,
    Sandbox,
    SandboxException,
    SandboxNotFoundException,
    SandboxQuery,
    SandboxState,
)

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import Resources, SandboxCapabilities

from .base import E2BFamilyProvider, SdkSandboxHandle

if TYPE_CHECKING:
    from evalspleef.spi.secrets import SecretResolver

DEFAULT_REGION = "us-phx-1"
_TEMPLATE_KIND = "novita_template"

# Paid-tier limits documented at https://docs.novita.ai/guides/sandbox-quota-limit
# (the product page's 24 h figure is unverified; see TECH-DESIGN section 8.2).
_QUOTA_CONCURRENCY = 100
_QUOTA_LIFETIME_S = 3 * 3600
_MAX_RESOURCES = Resources(cpus=8, memory_mb=8192, disk_mb=20480)

_EVAL_LEAD = "eval-lead"


def _novita_isolation_profile() -> IsolationProfile:
    """Isolation profile for novita-sandbox (TECH-DESIGN section 7.7).

    Novita does not publish how the sandbox is isolated, so the declared level
    is L3 and ``verified_isolation`` stays ``None`` until a real-host run of the
    conformance C-ISO-* subset fills it in.
    """
    return IsolationProfile(
        provider="novita-sandbox",
        declared_isolation=Isolation.L3,
        verified_isolation=None,
        invariants={
            "I1": InvariantEntry(
                how="a fresh instance per trial from an immutable template; "
                "kill after the trial; metadata carries lease/trial labels",
                probe="list(labels)==1 while alive, 0 after destroy",
            ),
            "I2": InvariantEntry(
                how="waived: single-use instance, the root disk is writable but the "
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
                "egress proxy address for allowlists",
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


class NovitaSandboxProvider(E2BFamilyProvider):
    """Sandbox provider backed by the Novita AI Agent Sandbox (SDK 2.x)."""

    name = "novita-sandbox"

    def __init__(self, instance: str, config: Mapping[str, Any], secrets: SecretResolver) -> None:
        super().__init__(instance, config, secrets)
        self._region = str(config.get("region", DEFAULT_REGION))

    # -- configuration ---------------------------------------------------------

    def _template_kind(self) -> str:
        return _TEMPLATE_KIND

    @property
    def region(self) -> str:
        return self._region

    def _domain(self) -> str:
        """Sandbox domain derived from the region (SDK-level endpoint)."""
        override = self.config.get("domain")
        if override:
            return str(override)
        return f"{self._region}.sandbox.novita.ai"

    def _client(self) -> Novita:
        """Build a fresh SDK client, resolving the API key at the moment of use.

        The key stays on the control plane (FR-ISO-02): it is used only for the
        vendor management API and never enters the sandbox.
        """
        ref = self._api_key_ref()
        api_key = self.secrets.resolve(ref) if ref is not None else None
        return Novita(api_key=api_key, domain=self._domain())

    def _capability_defaults(self) -> SandboxCapabilities:
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
            max_concurrency=_QUOTA_CONCURRENCY,
            max_lifetime_s=_QUOTA_LIFETIME_S,
            max_resources=_MAX_RESOURCES,
            typical_start_s=15.0,
            reset_cost_s=30.0,
        )

    def _profile(self) -> IsolationProfile:
        return _novita_isolation_profile()

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
        # lifecycle is left at the API default ('kill'): FR-SBX-04 forbids
        # on_timeout=pause / auto_resume because a paused instance could be
        # resumed and reused across trials (invariant I1).
        handle: SdkSandboxHandle = self._client().sandbox.create(
            template=template,
            timeout=timeout,
            metadata=dict(metadata),
            allow_internet_access=allow_internet_access,
            network=dict(network) if network else None,
        )
        return handle

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
        del name  # the novita SDK snapshots without a name parameter
        info = handle.create_snapshot()
        return str(info.snapshot_id)

    def _set_network(
        self, handle: Any, allow_out: list[str] | None, deny_out: list[str] | None
    ) -> None:
        handle.set_network(allow_out=allow_out, deny_out=deny_out)

    def _client_kwargs(self) -> dict[str, Any]:
        ref = self._api_key_ref()
        api_key = self.secrets.resolve(ref) if ref is not None else None
        return {"api_key": api_key, "domain": self._domain()}

    # -- vendor error types --------------------------------------------------------

    def _command_exit_exception(self) -> type[Exception]:
        exit_exception: type[Exception] = CommandExitException
        return exit_exception

    def _not_found_exceptions(self) -> tuple[type[Exception], ...]:
        return (SandboxNotFoundException,)

    def _rate_limit_exceptions(self) -> tuple[type[Exception], ...]:
        return (RateLimitException,)

    def _vendor_exceptions(self) -> tuple[type[Exception], ...]:
        return (SandboxException,)


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


__all__ = ["DEFAULT_REGION", "NovitaSandboxProvider"]
