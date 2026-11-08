from __future__ import annotations

from dataclasses import replace

import pytest
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.sandbox import Resources

from .conftest import EmptySecrets, build_spec

DIGEST = "sha256:" + "1" * 64


def _provider(**config: object) -> DockerProvider:
    return DockerProvider("docker-limits", config, EmptySecrets())


def test_a_lifetime_past_the_instance_ceiling_is_refused() -> None:
    provider = _provider(max_lifetime_s=600)
    provider._validate_spec(build_spec("t1", DIGEST, max_lifetime_s=600))
    with pytest.raises(CapabilityNotSupported, match="lifetime"):
        provider._validate_spec(build_spec("t2", DIGEST, max_lifetime_s=601))


def test_resources_past_the_configured_ceiling_are_refused() -> None:
    provider = _provider(max_cpus=2, max_disk_mb=2048)
    assert provider.capabilities().max_resources is not None
    spec = build_spec("t1", DIGEST)
    provider._validate_spec(replace(spec, resources=Resources(cpus=2, memory_mb=512, disk_mb=2048)))
    with pytest.raises(CapabilityNotSupported, match="disk_mb"):
        provider._validate_spec(
            replace(spec, resources=Resources(cpus=1, memory_mb=512, disk_mb=4096))
        )
    with pytest.raises(CapabilityNotSupported, match="cpus"):
        provider._validate_spec(
            replace(spec, resources=Resources(cpus=4, memory_mb=512, disk_mb=1024))
        )
    assert _provider().capabilities().max_resources is None


def test_the_workspace_is_sized_to_the_requested_disk() -> None:
    config = _provider()._container_config(build_spec("t1", DIGEST), "img", {})
    assert "size=1024m" in config["HostConfig"]["Tmpfs"]["/workspace"]
    assert "disk" in _provider().capabilities().enforces_limits


def test_only_verifier_sandboxes_get_a_writable_verify_root() -> None:
    provider = _provider()
    agent = provider._container_config(build_spec("t1", DIGEST), "img", {})
    verifier_spec = replace(build_spec("t1", DIGEST), role="verifier")
    verifier = provider._container_config(verifier_spec, "img", {})
    assert "/verify" not in agent["HostConfig"]["Tmpfs"]
    assert "/verify" in verifier["HostConfig"]["Tmpfs"]
