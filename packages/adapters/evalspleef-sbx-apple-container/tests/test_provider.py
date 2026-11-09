"""Provider-level tests against the fake container CLI."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from evalspleef_sbx_apple_container.fake_container import VERSION
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.sandbox import NetworkPolicy


async def test_capabilities(provider: AppleContainerProvider) -> None:
    caps = provider.capabilities()
    assert caps.os_families == frozenset({OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.ARM64})
    assert caps.isolation is Isolation.L3
    assert caps.net_modes == frozenset({NetMode.OFF, NetMode.FULL})
    assert not caps.allowlist_kinds
    assert caps.streaming and caps.stdin
    assert caps.snapshot == "none"
    assert not caps.gpu and caps.gui == "none" and not caps.port_expose
    assert not caps.host_mounts and not caps.dynamic_network_policy
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.max_concurrency == 4
    assert caps.max_lifetime_s == 7200


def test_egress_proxy_adds_whitelist_modes(make_provider) -> None:
    instance = make_provider(egress_proxy="proxy.internal:3128")
    assert instance.capabilities().net_modes == frozenset(
        {NetMode.OFF, NetMode.FULL, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR}
    )
    # Hostname filtering lives in the proxy; the provider must not claim it.
    assert not instance.capabilities().allowlist_kinds


async def test_preflight(provider: AppleContainerProvider) -> None:
    facts = await provider.preflight()
    # The real CLI prints one JSON object per component; the CLI's own entry wins.
    assert facts["version"] == VERSION
    assert facts["host_os"] == "darwin"
    assert facts["host_arch"] == Arch.ARM64.value


async def test_create_list_destroy_cycle(provider: AppleContainerProvider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("prov-1"))
    try:
        assert sandbox.id
        listed = await provider.list({"trial": "prov-1"})
        assert [entry["id"] for entry in listed] == [sandbox.id]
        labels = listed[0]["labels"]
        assert labels["spleef.provider"] == "adapter-test"
        assert int(labels["spleef.created_at"]) > 0
        assert int(labels["spleef.expires_at"]) > int(labels["spleef.created_at"])
    finally:
        await sandbox.destroy()
        await sandbox.destroy()  # idempotent
    assert await provider.list({"trial": "prov-1"}) == []


async def test_lifetime_label_is_clamped_to_the_capability(make_provider, spec_factory) -> None:
    instance = make_provider(max_lifetime_s=60)
    spec = spec_factory("prov-lifetime", max_lifetime_s=9999)
    sandbox = await instance.create(spec)
    try:
        labels = (await instance.list({"trial": "prov-lifetime"}))[0]["labels"]
        lifetime = int(labels["spleef.expires_at"]) - int(labels["spleef.created_at"])
        assert lifetime == 60
    finally:
        await sandbox.destroy()


async def test_reap_by_labels(provider: AppleContainerProvider, spec_factory) -> None:
    sandbox = await provider.create(spec_factory("prov-2"))
    reaped = await provider.reap({"trial": "prov-2"}, older_than_s=0)
    assert reaped == 1
    assert await provider.list({"trial": "prov-2"}) == []
    await sandbox.destroy()


async def test_max_concurrency(
    make_provider: AppleContainerProvider,
    spec_factory,
) -> None:
    limited = make_provider(max_concurrency=2)
    first = await limited.create(spec_factory("prov-3a"))
    second = await limited.create(spec_factory("prov-3b"))
    third_spec = spec_factory("prov-3c")
    try:
        with pytest.raises(QuotaExceeded):
            await limited.create(third_spec)
    finally:
        await first.destroy()
        await second.destroy()
    third = await limited.create(third_spec)
    await third.destroy()


async def test_create_fails_when_the_cli_refuses_the_name(
    provider: AppleContainerProvider, spec_factory
) -> None:
    occupied = await provider.cli.run(["create", "--name", "c-taken", "img", "sleep", "infinity"])
    assert occupied.exit_code == 0, occupied.stderr_text
    provider.pin_next_sandbox_id("c-taken")
    with pytest.raises(SandboxCreateError) as raised:
        await provider.create(spec_factory("prov-4a"))
    assert "already exists" in str(raised.value.detail)
    # The container holding the name is not the provider's: it must survive the failure.
    assert await provider.container_exists("c-taken")
    sandbox = await provider.create(spec_factory("prov-4b"))
    assert sandbox.id != "c-taken"
    await sandbox.destroy()


async def test_name_prefix_names_the_container(make_provider, spec_factory) -> None:
    instance = make_provider(name_prefix="lab-")
    sandbox = await instance.create(spec_factory("prov-prefix"))
    try:
        assert sandbox.id.startswith("lab-")
    finally:
        await sandbox.destroy()


class _Canary(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"canary")

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def host_canary() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Canary)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_off_network_cannot_reach_host_services(
    provider: AppleContainerProvider, spec_factory, host_canary: str
) -> None:
    # An --internal network is host-only on the real CLI; 'off' must not use it.
    probe = f"curl -fsS -o /dev/null --max-time 3 {host_canary}"
    full = await provider.create(spec_factory("prov-net-full", network=NetMode.FULL))
    try:
        assert (await full.exec(probe, timeout_s=10)).exit_code == 0
    finally:
        await full.destroy()
    off = await provider.create(spec_factory("prov-net-off"))
    try:
        assert (await off.exec(probe, timeout_s=10)).exit_code != 0
    finally:
        await off.destroy()


async def test_policy_rejections(provider: AppleContainerProvider, spec_factory) -> None:
    base = spec_factory("prov-5")
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(
            replace(
                base,
                trial_id="prov-5-docs",
                network=NetworkPolicy(mode=NetMode.DOCS_WHITELIST),
            )
        )
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(
            replace(
                base,
                trial_id="prov-5-cidr",
                network=NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",)),
            )
        )
    with pytest.raises(PolicyNotEnforceable):
        await provider.create(
            replace(
                base,
                trial_id="prov-5-host",
                network=NetworkPolicy(mode=NetMode.FULL, allow_hosts=("example.com",)),
            )
        )
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


async def test_whitelist_policy_uses_the_proxy(make_provider, spec_factory) -> None:
    instance = make_provider(egress_proxy="proxy.internal:3128")
    spec = spec_factory(
        "prov-6",
        network=NetMode.DOCS_WHITELIST,
        via_proxy="proxy.internal:3128",
    )
    sandbox = await instance.create(spec)
    try:
        proxy = await sandbox.exec('printf %s "$HTTPS_PROXY"')
        assert proxy.exit_code == 0
        assert proxy.stdout == b"http://proxy.internal:3128"
    finally:
        await sandbox.destroy()


async def test_whitelist_policy_without_via_proxy_is_rejected(make_provider, spec_factory) -> None:
    instance = make_provider(egress_proxy="proxy.internal:3128")
    spec = spec_factory("prov-7", network=NetMode.PACKAGE_MIRROR)
    with pytest.raises(PolicyNotEnforceable):
        await instance.create(spec)


async def test_sweep_removes_expired_containers(
    provider: AppleContainerProvider, spec_factory
) -> None:
    sandbox = await provider.create(spec_factory("prov-8", max_lifetime_s=0))
    await provider._sweep_once()
    assert await provider.list({"trial": "prov-8"}) == []
    await sandbox.destroy()


async def test_sweep_expired_reclaims_only_past_the_lifetime(
    provider: AppleContainerProvider, spec_factory
) -> None:
    sandbox = await provider.create(spec_factory("prov-ttl", max_lifetime_s=600))
    try:
        assert await provider.sweep_expired() == 0
        assert (await sandbox.exec(["true"])).exit_code == 0
        assert await provider.sweep_expired(now=time.time() + 601) == 1
        assert await provider.list({"trial": "prov-ttl"}) == []
        with pytest.raises(SandboxLost):
            await sandbox.exec(["true"])
    finally:
        await sandbox.destroy()


async def test_lose_sandbox_makes_exec_raise(
    provider: AppleContainerProvider, spec_factory
) -> None:
    sandbox = await provider.create(spec_factory("prov-9"))
    await provider.lose(sandbox.id)
    with pytest.raises(SandboxLost):
        await sandbox.exec(["true"])
    await sandbox.destroy()
