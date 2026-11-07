"""Provider-level tests against the real local Docker daemon."""

from __future__ import annotations

from dataclasses import replace

import pytest
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.sandbox import NetworkPolicy


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


async def test_capabilities(provider) -> None:
    caps = provider.capabilities()
    assert caps.os_families == frozenset({OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.ARM64})
    assert caps.isolation == Isolation.L1
    assert caps.net_modes == frozenset({NetMode.OFF, NetMode.FULL})
    assert not caps.allowlist_kinds
    assert caps.streaming and caps.stdin
    assert caps.snapshot == "none"
    assert not caps.gpu and caps.gui == "none" and not caps.port_expose
    assert not caps.host_mounts and not caps.dynamic_network_policy
    assert caps.enforces_limits == frozenset({"cpu", "memory", "pids"})
    assert caps.max_concurrency == 4
    assert caps.max_lifetime_s == 7200


async def test_preflight(provider) -> None:
    facts = await provider.preflight()
    assert facts["server_version"]
    assert facts["daemon_os"] == "linux"
    assert facts["host_arch"] == Arch.ARM64.value


async def test_create_list_destroy_cycle(provider, spec_factory) -> None:
    spec = spec_factory("prov-1")
    sandbox = await provider.create(spec)
    try:
        assert sandbox.id
        listed = await provider.list({"trial": "prov-1"})
        assert [entry["id"] for entry in listed] == [sandbox.id]
        labels = listed[0]["labels"]
        assert labels["spleef.provider"] == "adapter-test"
        assert int(labels["spleef.expires_at"]) > 0
    finally:
        await sandbox.destroy()
        await sandbox.destroy()  # idempotent
    assert await provider.list({"trial": "prov-1"}) == []


async def test_reap_by_labels(provider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("prov-2"))
    reaped = await provider.reap({"trial": "prov-2"}, older_than_s=0)
    assert reaped == 1
    assert await provider.list({"trial": "prov-2"}) == []
    # destroy on the already-removed container is still a success
    await sandbox.destroy()


async def test_max_concurrency(spec_factory) -> None:
    limited = DockerProvider(
        "limited",
        {"max_concurrency": 2, "pids_limit": 64},
        _Secrets(),
    )
    try:
        first = await limited.create(spec_factory("prov-3a"))
        second = await limited.create(spec_factory("prov-3b"))
        third_spec = spec_factory("prov-3c")
        try:
            with pytest.raises(QuotaExceeded):
                await limited.create(third_spec)
        finally:
            await first.destroy()
            await second.destroy()
        # a slot freed by destroy is usable again
        third = await limited.create(third_spec)
        await third.destroy()
    finally:
        await limited.close()


async def test_fail_next_create(provider, spec_factory) -> None:
    provider.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await provider.create(spec_factory("prov-4a"))
    sandbox = await provider.create(spec_factory("prov-4b"))
    await sandbox.destroy()


async def test_policy_rejections(provider, spec_factory) -> None:
    base = spec_factory("prov-5")
    docs = replace(
        base,
        trial_id="prov-5-docs",
        network=NetworkPolicy(mode=NetMode.DOCS_WHITELIST, allow_hosts=("docs.python.org",)),
    )
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(docs)
    full_cidr = replace(
        base,
        trial_id="prov-5-cidr",
        network=NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",)),
    )
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(full_cidr)
    with pytest.raises(CapabilityNotSupported):
        await provider.create(replace(base, trial_id="prov-5-os", os=OSFamily.WINDOWS))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(replace(base, trial_id="prov-5-arch", arch=Arch.X86_64))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(replace(base, trial_id="prov-5-gui", gui=True))
    with pytest.raises(CapabilityNotSupported):
        await provider.create(
            replace(
                base,
                trial_id="prov-5-tmpl",
                template=replace(base.template, kind="novita_template"),
            )
        )


async def test_sweep_removes_expired_containers(provider, spec_factory) -> None:
    spec = spec_factory("prov-6", max_lifetime_s=0)
    sandbox = await provider.create(spec)
    await provider._sweep_once()
    assert await provider.list({"trial": "prov-6"}) == []
    await sandbox.destroy()


async def test_lose_sandbox_makes_exec_raise(provider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("prov-7"))
    await provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()
