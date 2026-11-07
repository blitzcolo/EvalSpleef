"""Conformance targets for the E2B-family adapters, running against fakes.

Registered under the ``evalspleef.conformance_targets`` entry-point group as
``e2b-family``. Two targets are provided:

* ``novita-fake`` - :class:`NovitaSandboxProvider` with the ``novita-sandbox``
  SDK entry points patched to the in-memory fake;
* ``e2b-selfhost-fake`` - :class:`E2BSelfHostProvider` with the ``e2b`` SDK
  entry points patched to the same fake.

Both targets set ``fake=True``: they run real local subprocesses in temporary
directories and model vendor network policy with a blackhole proxy, so they
verify the adapter logic, not isolation. **Nothing here is verified against a
real Novita account or E2B cluster.**

Only this module may import ``evalspleef.conformance`` / ``evalspleef.testing``
(the root import-linter config exempts ``<adapter>.conformance`` for exactly
this); the production modules and the fakes stay on the SPI plus the vendor SDKs.
"""

from __future__ import annotations

from dataclasses import replace
from types import ModuleType

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


def build_targets() -> list[ConformanceTarget]:
    """The two fake conformance targets ('novita-fake', 'e2b-selfhost-fake')."""
    secrets = DictSecretResolver({})
    novita_provider = FakeNovitaSandboxProvider("novita-fake", {"region": "us-phx-1"}, secrets)
    selfhost_provider = FakeE2BSelfHostProvider(
        "e2b-selfhost-fake", {"domain": "e2b.fake.invalid"}, secrets
    )
    return [
        _build_target(
            "novita-fake", novita_provider, novita_module, "novita_template", FakeE2BCluster()
        ),
        _build_target(
            "e2b-selfhost-fake",
            selfhost_provider,
            selfhost_module,
            "e2b_template",
            FakeE2BCluster(),
        ),
    ]


__all__ = ["FakeE2BSelfHostProvider", "FakeNovitaSandboxProvider", "build_targets"]
