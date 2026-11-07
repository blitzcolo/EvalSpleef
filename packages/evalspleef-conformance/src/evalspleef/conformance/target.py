"""Conformance target contract.

A ``ConformanceTarget`` is one configured provider instance the conformance kit
runs cases against. Adapter packages expose targets through the
``evalspleef.conformance_targets`` entry-point group; each entry point names a
callable returning ``list[ConformanceTarget]``.

Targets that run against a fake of an external system (test doubles, recorded
transports) set ``fake=True`` so the report can distinguish
"verified on a real host" from "verified against a double".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from evalspleef.spi.caps import NetMode
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import ModelEndpointProvider, ModelSpec
from evalspleef.spi.sandbox import Sandbox, SandboxProvider, SandboxSpec
from evalspleef.testing.fake_openai import FakeOpenAIServer

Injector = Callable[..., Awaitable[None]]
"""An async fault injector used by fault cases.

Named injectors used by the builtin case library: ``lose_sandbox`` (the vendor
reclaims a live sandbox), ``fail_create`` (the next create raises),
``expire_lifetime`` (the sandbox exceeds ``max_lifetime_s``) for sandbox targets;
``upstream_status`` (the upstream answers with a given HTTP status) and
``kill_host`` (the host running the endpoint vanishes) for model targets.
"""

SpecFactory = Callable[..., SandboxSpec]
"""``(trial_id, *, network, max_lifetime_s) -> SandboxSpec`` for sandbox targets."""


@dataclass(frozen=True)
class ModelFixture:
    """Everything model cases (M-API, M-ID, M-LIFE, M-ASSIST, M-HF) need beside the provider.

    All fields are plain data or factories so a target stays cheap to construct;
    cases open and close any servers themselves inside their ``run``.
    """

    spec_factory: Callable[[], ModelSpec]
    """Builds the ``ModelSpec`` the provider is registered and deployed against."""

    operator_input_factory: Callable[[], Mapping[str, str]] | None
    """Assisted providers (M-ASSIST): the ``base_url`` / ``model`` / ``deployment_id``
    mapping an operator would paste back; ``None`` when the target is not assisted."""

    upstream: FakeOpenAIServer | None
    """Controls of the fake OpenAI-compatible upstream: scripted replies, injected
    error statuses and the reported ``model`` name (M-API, M-ID). ``None`` when the
    target talks to a real upstream."""

    hf_access: HFAccessChecker | None = None
    """Optional HF access double used by M-HF preflight cases (public, gated and
    private repos, mirror verification)."""


@dataclass(frozen=True)
class ConformanceTarget:
    """One provider instance under conformance testing."""

    name: str
    """Unique target name, selectable with ``--spleef-provider NAME``."""

    kind: Literal["sandbox", "model"]
    """Which SPI ``provider`` implements."""

    provider: SandboxProvider | ModelEndpointProvider
    """The configured provider instance under test."""

    fake: bool = False
    """True when the provider talks to a fake of an external system; recorded in the report."""

    spec_factory: SpecFactory | None = None
    """Sandbox targets: ``(trial_id, *, network, max_lifetime_s) -> SandboxSpec``."""

    injectors: Mapping[str, Injector] = field(default_factory=dict)
    """Optional async fault injectors (``lose_sandbox``, ``fail_create``,
    ``expire_lifetime``, ``upstream_status``, ``kill_host``) used by fault cases."""

    model_fixture: ModelFixture | None = None
    """Model targets: spec factory, operator input factory and upstream controls."""

    notes: str = ""
    """Free-form context shown in the report (for example what is faked)."""

    cleanup: Callable[[], Awaitable[None]] | None = None
    """Optional async teardown the plugin awaits after all cases of this target ran."""

    @asynccontextmanager
    async def open_sandbox(
        self,
        trial_id: str = "conf-1",
        *,
        network: NetMode = NetMode.OFF,
        max_lifetime_s: int = 600,
    ) -> AsyncIterator[Sandbox]:
        """Create a sandbox for one case body and always destroy it on exit.

        The sandbox is created through ``provider.create(spec_factory(...))`` and
        destroyed even when the body raises; the original exception propagates.
        """
        if self.kind != "sandbox":
            raise TypeError(f"open_sandbox requires a sandbox target, got {self.kind!r}")
        provider = self.provider
        if not isinstance(provider, SandboxProvider):
            raise TypeError(f"target {self.name}: provider is not a SandboxProvider")
        if self.spec_factory is None:
            raise TypeError(f"target {self.name}: sandbox target has no spec_factory")
        spec = self.spec_factory(trial_id, network=network, max_lifetime_s=max_lifetime_s)
        sandbox = await provider.create(spec)
        try:
            yield sandbox
        finally:
            await sandbox.destroy()
