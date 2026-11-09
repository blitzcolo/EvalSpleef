"""Proxy environment mapping of the Docker provider (no daemon needed)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef

_PROXY_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"}


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(ref)


def _spec(network: NetworkPolicy, env: dict[str, str] | None = None) -> SandboxSpec:
    return SandboxSpec(
        trial_id="proxy-env",
        lease_id="lease-proxy-env",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref="example:latest", digest="sha256:" + "0" * 64),
        resources=Resources(cpus=1, memory_mb=256, disk_mb=256),
        network=network,
        max_lifetime_s=600,
        env=env or {},
    )


def _env(provider: DockerProvider, spec: SandboxSpec) -> dict[str, str]:
    entries = provider._container_config(spec, "example:latest", {})["Env"]
    return dict(entry.split("=", 1) for entry in entries)


@pytest.mark.parametrize("via", ["egress:3128", "http://egress:3128"])
@pytest.mark.parametrize("mode", [NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR])
def test_proxied_modes_export_one_normalised_proxy_url(mode: NetMode, via: str) -> None:
    provider = DockerProvider("t", {"egress_network": "internal"}, _Secrets())
    env = _env(provider, _spec(NetworkPolicy(mode=mode, via_proxy=via)))
    assert {env[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")} == {
        "http://egress:3128"
    }
    assert env["NO_PROXY"] == env["no_proxy"]
    assert "127.0.0.1" in env["NO_PROXY"].split(",")


def test_policy_proxy_overrides_spec_env_and_keeps_other_entries() -> None:
    provider = DockerProvider("t", {"egress_network": "internal"}, _Secrets())
    spec = _spec(
        NetworkPolicy(mode=NetMode.DOCS_WHITELIST, via_proxy="egress:3128"),
        env={"HTTP_PROXY": "http://elsewhere:1", "KEEP": "1"},
    )
    env = _env(provider, spec)
    assert env["HTTP_PROXY"] == "http://egress:3128"
    assert env["KEEP"] == "1"


@pytest.mark.parametrize("mode", [NetMode.OFF, NetMode.FULL])
def test_off_and_full_modes_add_no_proxy_env(mode: NetMode) -> None:
    provider = DockerProvider("t", {"egress_network": "internal"}, _Secrets())
    spec = replace(_spec(NetworkPolicy(mode=mode, via_proxy="egress:3128")), env={"KEEP": "1"})
    assert _env(provider, spec) == {"KEEP": "1"}
    assert not _PROXY_NAMES & set(_env(provider, spec))
