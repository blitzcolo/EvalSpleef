"""Isolation profile, YAML artifact and probe execution checks."""

from __future__ import annotations

import re
from pathlib import Path

from evalspleef_sbx_k8s_agent_sandbox import isolation
from evalspleef_sbx_k8s_agent_sandbox.fake import FakeKubeApi
from evalspleef_sbx_k8s_agent_sandbox.provider import K8sAgentSandboxProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

PACKAGE_DIR = Path(isolation.__file__).parent
YAML_PATH = PACKAGE_DIR / "isolation_profile.yaml"
PROBE_PATH = PACKAGE_DIR / "probes" / "linux.sh"
INVARIANT_IDS = [f"I{index}" for index in range(1, 17)]


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


def _provider(runtime_class: str = "gvisor") -> K8sAgentSandboxProvider:
    return K8sAgentSandboxProvider(
        "isolation-test",
        {"runtime_class": runtime_class, "ready_poll_s": 0.01},
        _Secrets(),
        api=FakeKubeApi(),
    )


def test_profile_covers_all_invariants_without_waivers() -> None:
    profile = isolation.isolation_profile()
    assert profile.provider == "k8s-agent-sandbox"
    assert profile.declared_isolation is Isolation.L2
    assert set(profile.invariants) == set(INVARIANT_IDS)
    assert len(profile.invariants) == 16
    assert not profile.waivers()
    for entry in profile.invariants.values():
        assert entry.how
        assert entry.probe


def test_waivers_carry_reason_compensation_and_expiry() -> None:
    for entry in isolation.isolation_profile().waivers().values():
        assert entry.reason
        assert entry.compensating
        assert entry.expires


def test_yaml_matches_python_profile() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    yaml_keys = set(re.findall(r"^  (I\d+):$", text, flags=re.MULTILINE))
    profile = isolation.isolation_profile()
    assert yaml_keys == set(profile.invariants)
    assert "declared_isolation: L2" in text
    assert "provider: k8s-agent-sandbox" in text


def test_kata_profile_declares_l3() -> None:
    provider = _provider("kata-qemu")
    assert provider.capabilities().isolation is Isolation.L3
    assert provider.isolation_profile().declared_isolation is Isolation.L3


def test_linux_probe_script_checks_core_invariants() -> None:
    script = isolation.linux_probe_script()
    for needle in [
        "id -u",
        "sudo -n true",
        "findmnt -no OPTIONS /",
        "workspace",
        "docker.sock",
        "serviceaccount/token",
        "CapEff",
        "cpu.max",
        "memory.max",
        "RESULT pass",
        "RESULT fail",
    ]:
        assert needle in script, f"probe script misses {needle!r}"
    assert PROBE_PATH.read_text(encoding="utf-8") == script


async def test_probe_runs_through_exec() -> None:
    provider = _provider()
    spec = SandboxSpec(
        trial_id="probe-1",
        lease_id="lease-probe-1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(
            kind="oci",
            ref="ghcr.io/evalspleef/test-sandbox:latest",
            digest="sha256:" + "0" * 64,
        ),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=1024),
        network=NetworkPolicy(mode=NetMode.OFF),
        max_lifetime_s=60,
        labels={"batch": "probe", "trial": "probe-1", "lease": "lease-probe-1"},
    )
    sandbox = await provider.create(spec)
    try:
        result = await provider.run_probe(sandbox)
        assert b"RESULT " in result.stdout
        assert result.exit_code in (0, 1)
    finally:
        await sandbox.destroy()
