"""Shared fixtures: providers running against the in-memory SDK fake."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from evalspleef_sbx_e2b_family import novita as novita_module
from evalspleef_sbx_e2b_family import selfhost as selfhost_module
from evalspleef_sbx_e2b_family.fakes import (
    FakeE2BCluster,
    patch_adapter_module,
    unpatch_adapter_module,
)

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.secrets import DictSecretResolver

DIGEST = "sha256:" + "cd" * 32


def make_spec(
    trial_id: str = "trial-1",
    *,
    network: NetMode = NetMode.OFF,
    kind: str = "novita_template",
    labels: dict[str, str] | None = None,
    max_lifetime_s: int = 600,
) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind=kind, ref="tpl-test", digest=DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=20480),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels=labels or {"batch": "test", "trial": trial_id},
    )


@pytest.fixture
def novita_cluster(tmp_path: Path) -> Iterator[FakeE2BCluster]:
    yield FakeE2BCluster(base_dir=tmp_path)


@pytest.fixture
def novita_provider(
    monkeypatch: pytest.MonkeyPatch, novita_cluster: FakeE2BCluster
) -> Iterator[novita_module.NovitaSandboxProvider]:
    originals = patch_adapter_module(novita_module, novita_cluster)
    provider = novita_module.NovitaSandboxProvider(
        "novita-test",
        {"region": "us-phx-1", "api_key_ref": "vault://novita/test"},
        DictSecretResolver({"vault://novita/test": "sk-test-novita-key"}),
    )
    try:
        yield provider
    finally:
        novita_cluster.cleanup()
        unpatch_adapter_module(novita_module, originals)


@pytest.fixture
def selfhost_cluster(tmp_path: Path) -> Iterator[FakeE2BCluster]:
    yield FakeE2BCluster(base_dir=tmp_path)


@pytest.fixture
def selfhost_provider(
    selfhost_cluster: FakeE2BCluster,
) -> Iterator[selfhost_module.E2BSelfHostProvider]:
    cluster = selfhost_cluster
    originals = patch_adapter_module(selfhost_module, cluster)
    provider = selfhost_module.E2BSelfHostProvider(
        "e2b-test",
        {"domain": "e2b.test.invalid", "api_key_ref": "env://E2B_API_KEY"},
        DictSecretResolver({"env://E2B_API_KEY": "sk-test-e2b-key"}),
    )
    try:
        yield provider
    finally:
        cluster.cleanup()
        unpatch_adapter_module(selfhost_module, originals)
