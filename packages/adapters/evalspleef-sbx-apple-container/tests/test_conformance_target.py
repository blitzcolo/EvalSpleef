"""The conformance target wiring: the fake target, its injectors and the live gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from evalspleef_sbx_apple_container import conformance
from evalspleef_sbx_apple_container.conformance import build_targets
from evalspleef_sbx_apple_container.provider import AppleContainerProvider

from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import SandboxCreateError, SandboxLost
from evalspleef.spi.sandbox import Sandbox


async def _cleanup(targets: list[ConformanceTarget]) -> None:
    for target in targets:
        assert target.cleanup is not None
        await target.cleanup()


async def test_fake_target_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(conformance.LIVE_IMAGE_ENV, raising=False)
    targets = build_targets()
    assert [target.name for target in targets] == ["apple-container-fake"]
    target = targets[0]
    try:
        assert target.kind == "sandbox"
        assert target.fake is True
        assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
        assert target.spec_factory is not None
        assert getattr(target, "reachable_from_sandbox", None) == "127.0.0.1"
        async with target.open_sandbox("target-1") as sandbox:
            assert isinstance(sandbox, Sandbox)
            result = await sandbox.exec(["printf", "target-ok"])
            assert result.stdout == b"target-ok"
    finally:
        await _cleanup(targets)


async def test_fault_injectors_drive_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(conformance.LIVE_IMAGE_ENV, raising=False)
    targets = build_targets()
    target = targets[0]
    provider = target.provider
    assert isinstance(provider, AppleContainerProvider)
    try:
        async with target.open_sandbox("target-lose") as sandbox:
            await target.injectors["lose_sandbox"](sandbox)
            with pytest.raises(SandboxLost):
                await sandbox.exec(["true"])
        async with target.open_sandbox("target-expire") as sandbox:
            await target.injectors["expire_lifetime"]()
            with pytest.raises(SandboxLost):
                await sandbox.exec(["true"])
        await target.injectors["fail_create"]()
        assert target.spec_factory is not None
        spec = target.spec_factory("target-fail", network=NetMode.OFF, max_lifetime_s=60)
        with pytest.raises(SandboxCreateError):
            await provider.create(spec)
        async with target.open_sandbox("target-retry") as sandbox:
            assert (await sandbox.exec(["true"])).exit_code == 0
    finally:
        await _cleanup(targets)


async def test_live_target_needs_the_cli_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(conformance.LIVE_IMAGE_ENV, "docker.io/library/python:3.12-slim")
    monkeypatch.setenv("PATH", str(tmp_path))
    targets = build_targets()
    try:
        assert [target.name for target in targets] == ["apple-container-fake"]
    finally:
        await _cleanup(targets)


async def test_live_target_needs_the_image_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(conformance.LIVE_IMAGE_ENV, raising=False)
    fake_cli = tmp_path / "container"
    fake_cli.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    targets = build_targets()
    try:
        assert [target.name for target in targets] == ["apple-container-fake"]
    finally:
        await _cleanup(targets)
