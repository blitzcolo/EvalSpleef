"""The conformance target wiring for the local Docker daemon."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from evalspleef_sbx_docker.conformance import build_targets


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="no working local Docker daemon")
def test_local_docker_target() -> None:
    targets = build_targets()
    assert [target.name for target in targets] == ["local-docker"]
    target = targets[0]
    assert target.kind == "sandbox"
    assert target.fake is False
    assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
    assert target.cleanup is not None
    assert target.spec_factory is not None
    assert target.reachable_from_sandbox, "no host gateway address detected"
