"""Lifecycle, network mapping, quotas and capability limits of the providers."""

from __future__ import annotations

import asyncio

import pytest
from evalspleef_sbx_e2b_family import novita as novita_module
from evalspleef_sbx_e2b_family import selfhost as selfhost_module
from evalspleef_sbx_e2b_family.fakes import (
    FakeRateLimitException,
    FakeSandboxException,
)

from evalspleef.spi.caps import Isolation, NetMode
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.sandbox import NetworkPolicy, Resources

from .conftest import make_spec

# -- capabilities and config -------------------------------------------------


def test_novita_capabilities_and_config() -> None:
    provider = novita_module.NovitaSandboxProvider("novita-us-phx-1", {"region": "us-phx-1"}, {})
    caps = provider.capabilities()
    assert provider.region == "us-phx-1"
    assert provider._domain() == "us-phx-1.sandbox.novita.ai"
    assert caps.isolation is Isolation.L3
    assert caps.max_concurrency == 100
    assert caps.max_lifetime_s == 10800
    assert caps.max_resources == Resources(cpus=8, memory_mb=8192, disk_mb=20480)
    assert caps.snapshot == "memory" and caps.snapshot_fanout


def test_novita_capabilities_limits_from_config() -> None:
    provider = novita_module.NovitaSandboxProvider(
        "inst",
        {"limits": {"max_concurrency": 80, "max_lifetime_s": 7200}},
        {},
    )
    caps = provider.capabilities()
    assert caps.max_concurrency == 80
    assert caps.max_lifetime_s == 7200


def test_selfhost_domain_from_env(monkeypatch) -> None:
    monkeypatch.setenv("E2B_DOMAIN", "e2b.internal.example")
    provider = selfhost_module.E2BSelfHostProvider("e2b-vpc", {}, {})
    assert provider.domain == "e2b.internal.example"


def test_selfhost_domain_config_wins(monkeypatch) -> None:
    monkeypatch.setenv("E2B_DOMAIN", "e2b.internal.example")
    provider = selfhost_module.E2BSelfHostProvider("e2b-vpc", {"domain": "e2b.other.example"}, {})
    assert provider.domain == "e2b.other.example"


def test_selfhost_uncapped_by_default() -> None:
    provider = selfhost_module.E2BSelfHostProvider("e2b-vpc", {}, {})
    caps = provider.capabilities()
    assert caps.max_concurrency is None
    assert caps.max_lifetime_s is None
    assert caps.max_resources is None


# -- spec validation ------------------------------------------------------------


async def test_create_rejects_wrong_template_kind(novita_provider) -> None:
    with pytest.raises(CapabilityNotSupported):
        await novita_provider.create(make_spec(kind="e2b_template"))


async def test_create_rejects_oversized_lifetime(novita_provider) -> None:
    with pytest.raises(CapabilityNotSupported):
        await novita_provider.create(make_spec(max_lifetime_s=10801))


async def test_create_rejects_oversized_resources(novita_provider) -> None:
    spec = make_spec()
    spec = spec.__class__(
        **{
            **spec.__dict__,
            "resources": Resources(cpus=16, memory_mb=4096, disk_mb=20480),
        }
    )
    with pytest.raises(CapabilityNotSupported):
        await novita_provider.create(spec)


async def test_create_rejects_unsupported_network_mode(novita_provider, monkeypatch) -> None:
    caps = novita_provider.capabilities()
    monkeypatch.setattr(
        novita_provider,
        "_capability_defaults",
        lambda: caps.__class__(
            **{**caps.__dict__, "net_modes": frozenset({NetMode.OFF, NetMode.FULL})}
        ),
    )
    spec = make_spec(network=NetMode.DOCS_WHITELIST)
    with pytest.raises(PolicyNotEnforceable):
        await novita_provider.create(spec)


# -- network mapping ---------------------------------------------------------------


async def test_network_off_maps_to_allow_internet_access_false(
    novita_provider, novita_cluster
) -> None:
    sandbox = await novita_provider.create(make_spec(network=NetMode.OFF))
    try:
        call = novita_cluster.create_calls[-1]
        assert call["allow_internet_access"] is False
        assert call["network"] is None
    finally:
        await sandbox.destroy()


async def test_network_full_is_default(novita_provider, novita_cluster) -> None:
    sandbox = await novita_provider.create(make_spec(network=NetMode.FULL))
    try:
        call = novita_cluster.create_calls[-1]
        assert call["allow_internet_access"] is True
        assert call["network"] is None
    finally:
        await sandbox.destroy()


async def test_network_allowlist_maps_to_allow_out(novita_provider, novita_cluster) -> None:
    spec = make_spec(network=NetMode.DOCS_WHITELIST)
    spec = spec.__class__(
        **{
            **spec.__dict__,
            "network": NetworkPolicy(
                mode=NetMode.DOCS_WHITELIST,
                allow_hosts=("docs.python.org", "*.readthedocs.io"),
                allow_cidrs=("203.0.113.0/24",),
            ),
        }
    )
    sandbox = await novita_provider.create(spec)
    try:
        call = novita_cluster.create_calls[-1]
        assert call["allow_internet_access"] is True
        assert call["network"] == {
            "allow_out": ["docs.python.org", "*.readthedocs.io", "203.0.113.0/24"]
        }
    finally:
        await sandbox.destroy()


async def test_network_via_proxy_only_allows_proxy(novita_provider, novita_cluster) -> None:
    spec = make_spec(network=NetMode.PACKAGE_MIRROR)
    spec = spec.__class__(
        **{
            **spec.__dict__,
            "network": NetworkPolicy(
                mode=NetMode.PACKAGE_MIRROR,
                allow_hosts=("pypi.org",),
                via_proxy="http://egress.internal:3128",
            ),
        }
    )
    sandbox = await novita_provider.create(spec)
    try:
        call = novita_cluster.create_calls[-1]
        assert call["network"] == {"allow_out": ["egress.internal"]}
    finally:
        await sandbox.destroy()


async def test_allowlist_without_hosts_is_rejected(novita_provider) -> None:
    spec = make_spec(network=NetMode.DOCS_WHITELIST)
    with pytest.raises(PolicyNotEnforceable):
        await novita_provider.create(spec)


async def test_set_network_runtime_change(novita_provider, novita_cluster) -> None:
    sandbox = await novita_provider.create(make_spec(network=NetMode.OFF))
    try:
        record = novita_cluster.peek(sandbox.id)
        assert record is not None
        await sandbox.set_network(NetworkPolicy(mode=NetMode.FULL))
        assert record.network == {"allow_out": ["0.0.0.0/0"]}
        await sandbox.set_network(NetworkPolicy(mode=NetMode.OFF))
        assert record.network == {"deny_out": ["0.0.0.0/0"]}
        await sandbox.set_network(
            NetworkPolicy(mode=NetMode.DOCS_WHITELIST, allow_hosts=("a.example",))
        )
        assert record.network == {"allow_out": ["a.example"]}
    finally:
        await sandbox.destroy()


# -- fault mapping and quotas -------------------------------------------------------


async def test_create_failure_maps_to_sandbox_create_error(novita_provider, novita_cluster) -> None:
    novita_cluster.fail_next_creates = 1
    novita_cluster.fail_create_error = FakeSandboxException
    with pytest.raises(SandboxCreateError):
        await novita_provider.create(make_spec("retry-a"))
    # retry works
    sandbox = await novita_provider.create(make_spec("retry-b"))
    probe = await sandbox.exec(["true"])
    assert probe.exit_code == 0
    await sandbox.destroy()


async def test_rate_limit_maps_to_quota_exceeded(novita_provider, novita_cluster) -> None:
    novita_cluster.fail_next_creates = 1
    novita_cluster.fail_create_error = FakeRateLimitException
    with pytest.raises(QuotaExceeded):
        await novita_provider.create(make_spec("rl-a"))


async def test_concurrency_limit_raises_quota_exceeded(novita_provider, monkeypatch) -> None:
    caps = novita_provider.capabilities()
    monkeypatch.setattr(
        novita_provider,
        "_capability_defaults",
        lambda: caps.__class__(**{**caps.__dict__, "max_concurrency": 1}),
    )
    first = await novita_provider.create(make_spec("conc-a"))
    try:
        with pytest.raises(QuotaExceeded):
            await novita_provider.create(make_spec("conc-b"))
    finally:
        await first.destroy()
    second = await novita_provider.create(make_spec("conc-c"))
    await second.destroy()


async def test_concurrent_creates_cannot_overrun_the_limit(novita_provider, monkeypatch) -> None:
    """Creates in flight count: a live Novita run created limit + 1 sandboxes without this."""
    caps = novita_provider.capabilities()
    monkeypatch.setattr(
        novita_provider,
        "_capability_defaults",
        lambda: caps.__class__(**{**caps.__dict__, "max_concurrency": 2}),
    )
    results = await asyncio.gather(
        *(novita_provider.create(make_spec(f"race-{n}")) for n in range(3)),
        return_exceptions=True,
    )
    created = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, QuotaExceeded)]
    try:
        assert (len(created), len(refused)) == (2, 1)
    finally:
        for sandbox in created:
            await sandbox.destroy()
    # A failed create gives its reservation back.
    again = await novita_provider.create(make_spec("race-after"))
    await again.destroy()


# -- list / reap ---------------------------------------------------------------------


async def test_list_and_reap_by_labels(novita_provider) -> None:
    spec_a = make_spec("list-a", labels={"batch": "list-test", "trial": "list-a"})
    spec_b = make_spec("list-b", labels={"batch": "list-test", "trial": "list-b"})
    sandbox_a = await novita_provider.create(spec_a)
    sandbox_b = await novita_provider.create(spec_b)
    try:
        listed = await novita_provider.list({"batch": "list-test"})
        assert {entry["id"] for entry in listed} == {sandbox_a.id, sandbox_b.id}
        # a narrower label query matches only that sandbox
        narrow = await novita_provider.list({"trial": "list-a"})
        assert [entry["id"] for entry in narrow] == [sandbox_a.id]
        reaped = await novita_provider.reap({"batch": "list-test"}, older_than_s=0)
        assert reaped == 2
        assert await novita_provider.list({"batch": "list-test"}) == []
    finally:
        await sandbox_a.destroy()
        await sandbox_b.destroy()


async def test_reap_only_older_than(novita_provider, monkeypatch) -> None:
    sandbox = await novita_provider.create(make_spec("reap-age", labels={"batch": "age"}))
    try:
        assert await novita_provider.reap({"batch": "age"}, older_than_s=10_000) == 0
        assert (await novita_provider.list({"batch": "age"}))[0]["id"] == sandbox.id
    finally:
        await sandbox.destroy()


async def test_destroy_is_idempotent_and_unlists(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec("die", labels={"batch": "die"}))
    await sandbox.destroy()
    await sandbox.destroy()  # second destroy must succeed
    assert await novita_provider.list({"batch": "die"}) == []


async def test_snapshot_roundtrip(novita_provider) -> None:
    sandbox = await novita_provider.create(make_spec("snap"))
    try:
        write = await sandbox.exec(["sh", "-c", "printf snapshot-body > marker.txt"])
        assert write.exit_code == 0
        snapshot_id = await sandbox.snapshot("conf-snap")
        assert snapshot_id
    finally:
        await sandbox.destroy()
    restored = await novita_provider.create_from_snapshot(snapshot_id, make_spec("snap-b"))
    try:
        probe = await restored.exec(["cat", "marker.txt"])
        assert probe.exit_code == 0
        assert probe.stdout == b"snapshot-body"
    finally:
        await restored.destroy()


async def test_lost_sandbox_exec_raises_sandbox_lost(novita_provider, novita_cluster) -> None:
    sandbox = await novita_provider.create(make_spec("lost"))
    novita_cluster.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()


async def test_api_key_never_reaches_sandbox(novita_provider, novita_cluster) -> None:
    sandbox = await novita_provider.create(make_spec("key-check"))
    try:
        assert novita_cluster.create_calls, "fake SDK was not used"
        for call in novita_cluster.create_calls:
            blob = repr(call)
            assert "sk-test-novita-key" not in blob
        leaked = await sandbox.exec(
            "env | grep -i novita || ls /home/agent 2>/dev/null | grep -i novita || true"
        )
        assert leaked.exit_code == 0
        assert b"sk-test" not in leaked.stdout
    finally:
        await sandbox.destroy()


# -- e2b-selfhost specifics ----------------------------------------------------------


async def test_selfhost_allowlist_maps_to_typed_network_options(
    selfhost_provider, selfhost_cluster
) -> None:
    spec = make_spec(network=NetMode.DOCS_WHITELIST, kind="e2b_template")
    spec = spec.__class__(
        **{
            **spec.__dict__,
            "network": NetworkPolicy(mode=NetMode.DOCS_WHITELIST, allow_hosts=("docs.python.org",)),
        }
    )
    sandbox = await selfhost_provider.create(spec)
    try:
        call = selfhost_cluster.create_calls[-1]
        assert call["network"] == {"allow_out": ["docs.python.org"]}
        assert call["allow_internet_access"] is True
        assert call["timeout"] == spec.max_lifetime_s + 300
    finally:
        await sandbox.destroy()


async def test_selfhost_set_network_uses_update_network(
    selfhost_provider, selfhost_cluster
) -> None:
    sandbox = await selfhost_provider.create(make_spec(kind="e2b_template", network=NetMode.OFF))
    try:
        record = selfhost_cluster.peek(sandbox.id)
        assert record is not None
        await sandbox.set_network(
            NetworkPolicy(mode=NetMode.DOCS_WHITELIST, allow_hosts=("a.example",))
        )
        assert record.network == {"allow_out": ["a.example"]}
        await sandbox.set_network(NetworkPolicy(mode=NetMode.OFF))
        assert record.network == {"deny_out": ["0.0.0.0/0"]}
    finally:
        await sandbox.destroy()


async def test_selfhost_snapshot_roundtrip(selfhost_provider) -> None:
    sandbox = await selfhost_provider.create(make_spec("snap-sh", kind="e2b_template"))
    try:
        write = await sandbox.exec(["sh", "-c", "printf body > marker.txt"])
        assert write.exit_code == 0
        snapshot_id = await sandbox.snapshot("base")
    finally:
        await sandbox.destroy()
    restored = await selfhost_provider.create_from_snapshot(
        snapshot_id, make_spec("snap-sh-b", kind="e2b_template")
    )
    try:
        probe = await restored.exec(["cat", "marker.txt"])
        assert probe.stdout == b"body"
    finally:
        await restored.destroy()
