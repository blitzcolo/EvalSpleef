"""Conformance target for the libvirt adapter.

The target is deliberately named ``libvirt-fake`` and reports ``fake=True``: it
drives fake ``virsh`` and ``qemu-img`` executables (real subprocesses on a
temporary PATH) and a real local asyncssh server as the guest, so lifecycle,
exec, SFTP and fault handling run end to end while no hypervisor or real guest
is involved. Nothing here is verified on a real host.

    pytest -p evalspleef.conformance.plugin --spleef-provider libvirt-fake \\
        --spleef-report /tmp/spleef-libvirt-fake-report.json \\
        packages/adapters/evalspleef-sbx-libvirt

Fault injectors: ``lose_sandbox`` (destroy the domain behind a live sandbox),
``expire_lifetime`` (destroy every live sandbox, mirroring the TTL sweep) and
``fail_create`` (the next create raises ``SandboxCreateError``).
"""

from __future__ import annotations

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.errors import SandboxCreateError
from evalspleef.spi.sandbox import Sandbox

from ._fake import FakeLibvirtHost


def build_targets() -> list[ConformanceTarget]:
    """The ``libvirt-fake`` target: fake hypervisor CLI, real local SSH guest."""
    host = FakeLibvirtHost().start()
    provider = host.provider()

    async def lose_sandbox(sandbox: Sandbox) -> None:
        await provider.lose(sandbox.id)

    async def expire_lifetime() -> None:
        await provider.expire_live_sandboxes()

    async def fail_create(count: int = 1) -> None:
        provider.fail_next_creates(count, SandboxCreateError)

    async def cleanup() -> None:
        await provider.close()
        host.stop()

    target = ConformanceTarget(
        name="libvirt-fake",
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=host.spec_factory,
        injectors={
            "lose_sandbox": lose_sandbox,
            "expire_lifetime": expire_lifetime,
            "fail_create": fail_create,
        },
        notes=(
            "fake virsh/qemu-img executables plus a real local asyncssh guest; "
            "not verified on a real libvirt host"
        ),
        cleanup=cleanup,
    )
    return [target]
