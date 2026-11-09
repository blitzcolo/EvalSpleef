"""The CLI's provider chain (registry wrapper -> prose-probe bridge) keeps tracing intact."""

from __future__ import annotations

from evalspleef.control.cli.context import _BridgedProvider
from evalspleef.control.telemetry import traced_provider
from evalspleef.control.telemetry.tracing import TracedSandbox
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver


async def test_bridge_over_traced_provider_keeps_name_instance_and_traced_sandboxes() -> None:
    inner = MockSandboxProvider("docker-a", {}, DictSecretResolver())
    traced = traced_provider(inner)
    bridged = _BridgedProvider(traced, inner.isolation_profile())

    # trial.versions records the adapter name: the bridge reads it from the traced wrapper
    assert bridged.name == inner.name
    assert bridged.instance == "docker-a"
    assert bridged.secrets is inner.secrets

    spec = SandboxSpec(
        trial_id="logic-001.demo.model.shell.e1.t01",
        lease_id="lease-1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="registry.test/x:1", digest="sha256:" + "a" * 64),
        resources=Resources(cpus=1, memory_mb=256, disk_mb=512),
        network=NetworkPolicy(mode=NetMode.OFF),
    )
    sandbox = await bridged.create(spec)
    assert isinstance(sandbox, TracedSandbox)
    await sandbox.destroy()
