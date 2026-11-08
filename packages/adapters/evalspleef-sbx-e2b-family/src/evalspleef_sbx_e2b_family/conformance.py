# mypy: disable-error-code=import-untyped
"""Conformance targets for the E2B-family adapters, running against fakes.

Registered under the ``evalspleef.conformance_targets`` entry-point group as
``e2b-family``. Two targets are provided:

* ``novita-fake`` - :class:`NovitaSandboxProvider` with the ``novita-sandbox``
  SDK entry points patched to the in-memory fake;
* ``e2b-selfhost-fake`` - :class:`E2BSelfHostProvider` with the ``e2b`` SDK
  entry points patched to the same fake.

Both targets set ``fake=True``: they run real local subprocesses in temporary
directories and model vendor network policy with a blackhole proxy, so they
verify the adapter logic, not isolation.

A third target, ``novita-live``, runs the real adapter against a real Novita
account. It is registered only when ``NOVITA_API_KEY`` and
``SPLEEF_NOVITA_TEMPLATE`` (a template built by the Template Builder, with its
image digest in ``SPLEEF_NOVITA_TEMPLATE_DIGEST``) are set, and it creates and
destroys real, billed sandboxes. Create failures and lifetime expiry cannot be
injected into a real account, so those cases report not applicable there. The
fake patches the adapter module itself, so while ``novita-live`` is configured
``novita-fake`` is not built: one process cannot hold both.

Only this module may import ``evalspleef.conformance`` / ``evalspleef.testing``
(the root import-linter config exempts ``<adapter>.conformance`` for exactly
this); the production modules and the fakes stay on the SPI plus the vendor SDKs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import ModuleType

from novita_sandbox import Sandbox as NovitaSdkSandbox

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxSpec,
    TemplateKind,
    TemplateRef,
)
from evalspleef.testing.secrets import DictSecretResolver
from evalspleef_sbx_e2b_family import novita as novita_module
from evalspleef_sbx_e2b_family import selfhost as selfhost_module
from evalspleef_sbx_e2b_family.base import E2BFamilyProvider
from evalspleef_sbx_e2b_family.fakes import (
    FakeE2BCluster,
    FakeSandboxException,
    patch_adapter_module,
    unpatch_adapter_module,
)

_TEMPLATE_DIGEST = "sha256:" + "ab" * 32
_FAKE_NOTES = (
    "in-memory SDK double: real local subprocesses in a temp directory, "
    "network 'off' modelled via a blackhole proxy; no isolation, not verified "
    "on a real Novita account or E2B cluster"
)


def _spec_factory(
    trial_id: str, *, network: NetMode, max_lifetime_s: int, kind: TemplateKind
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind=kind, ref="tpl-conformance", digest=_TEMPLATE_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=20480),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={"conformance": "e2b-family", "trial": trial_id},
    )


def _as_double(caps: SandboxCapabilities) -> SandboxCapabilities:
    """Capability adjustments for the in-process double.

    The fake does not model vendor-side cgroup enforcement or account
    concurrency, so it declares neither; the C-ISO-05/C-CONC-01 cases then
    report not-applicable instead of passing vacuously against the double.
    """
    return replace(caps, enforces_limits=frozenset(), max_concurrency=None)


class FakeNovitaSandboxProvider(novita_module.NovitaSandboxProvider):
    """novita-sandbox provider bound to the fake SDK (conformance only)."""

    def _capability_defaults(self) -> SandboxCapabilities:
        return _as_double(super()._capability_defaults())


class FakeE2BSelfHostProvider(selfhost_module.E2BSelfHostProvider):
    """e2b-selfhost provider bound to the fake SDK (conformance only)."""

    def _capability_defaults(self) -> SandboxCapabilities:
        return _as_double(super()._capability_defaults())


def _build_target(
    name: str,
    provider: E2BFamilyProvider,
    adapter_module: ModuleType,
    kind: TemplateKind,
    cluster: FakeE2BCluster,
) -> ConformanceTarget:
    async def lose_sandbox(sandbox: Sandbox) -> None:
        cluster.lose(sandbox.id)

    async def fail_create(count: int = 1) -> None:
        cluster.fail_next_creates = count
        cluster.fail_create_error = FakeSandboxException

    async def expire_lifetime() -> None:
        cluster.expire_all()

    def spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return _spec_factory(trial_id, network=network, max_lifetime_s=max_lifetime_s, kind=kind)

    originals = patch_adapter_module(adapter_module, cluster)

    async def cleanup() -> None:
        unpatch_adapter_module(adapter_module, originals)
        cluster.cleanup()

    return ConformanceTarget(
        name=name,
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=spec_factory,
        injectors={
            "lose_sandbox": lose_sandbox,
            "fail_create": fail_create,
            "expire_lifetime": expire_lifetime,
        },
        notes=_FAKE_NOTES,
        cleanup=cleanup,
    )


def _novita_live_target(api_key: str, template: str, digest: str) -> ConformanceTarget:
    ref = "env://NOVITA_API_KEY"
    provider = novita_module.NovitaSandboxProvider(
        "novita-live",
        # A small declared limit keeps C-CONC-01 to a handful of billed sandboxes.
        {"api_key_ref": ref, "limits": {"max_concurrency": 5}},
        DictSecretResolver({ref: api_key}),
    )

    async def lose_sandbox(sandbox: Sandbox) -> None:
        # The vendor reclaiming a sandbox, out of band of the adapter's own teardown.
        await asyncio.to_thread(NovitaSdkSandbox.kill, sandbox.id, **provider._client_kwargs())

    def spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        spec = _spec_factory(
            trial_id, network=network, max_lifetime_s=max_lifetime_s, kind="novita_template"
        )
        return replace(
            spec,
            template=TemplateRef(kind="novita_template", ref=template, digest=digest),
            resources=Resources(cpus=2, memory_mb=2048, disk_mb=20480),
        )

    async def cleanup() -> None:
        await provider.reap({"conformance": "e2b-family"}, older_than_s=0)

    return ConformanceTarget(
        name="novita-live",
        kind="sandbox",
        provider=provider,
        fake=False,
        spec_factory=spec_factory,
        injectors={"lose_sandbox": lose_sandbox},
        notes="real Novita Agent Sandbox account; billed sandboxes from template " + template,
        cleanup=cleanup,
    )


def build_targets() -> list[ConformanceTarget]:
    """The fake targets, plus 'novita-live' when a real account and template are configured."""
    secrets = DictSecretResolver({})
    selfhost_provider = FakeE2BSelfHostProvider(
        "e2b-selfhost-fake", {"domain": "e2b.fake.invalid"}, secrets
    )
    targets = [
        _build_target(
            "e2b-selfhost-fake",
            selfhost_provider,
            selfhost_module,
            "e2b_template",
            FakeE2BCluster(),
        )
    ]
    api_key, template = os.environ.get("NOVITA_API_KEY"), os.environ.get("SPLEEF_NOVITA_TEMPLATE")
    if api_key and template:
        digest = os.environ.get("SPLEEF_NOVITA_TEMPLATE_DIGEST", _TEMPLATE_DIGEST)
        # The real SDK must stay unpatched, so the novita fake is not built alongside it.
        return [_novita_live_target(api_key, template, digest), *targets]
    novita_provider = FakeNovitaSandboxProvider("novita-fake", {"region": "us-phx-1"}, secrets)
    return [
        _build_target(
            "novita-fake", novita_provider, novita_module, "novita_template", FakeE2BCluster()
        ),
        *targets,
    ]


__all__ = ["FakeE2BSelfHostProvider", "FakeNovitaSandboxProvider", "build_targets"]
