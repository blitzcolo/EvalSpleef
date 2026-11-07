"""The conformance target wiring for the fake ``wsb`` harness."""

from __future__ import annotations

import os

from evalspleef_sbx_windows_sandbox.conformance import TARGET_NAME, build_targets
from evalspleef_sbx_windows_sandbox.provider import WindowsSandboxProvider

from evalspleef.spi.caps import NetMode, OSFamily
from evalspleef.spi.sandbox import SandboxProvider


async def test_fake_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    targets = build_targets()
    assert [target.name for target in targets] == [TARGET_NAME]
    target = targets[0]
    assert target.kind == "sandbox"
    assert target.fake is True
    assert isinstance(target.provider, WindowsSandboxProvider)
    assert isinstance(target.provider, SandboxProvider)
    assert target.provider.dev_only is True
    assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
    assert target.cleanup is not None
    assert target.spec_factory is not None
    spec = target.spec_factory("conf-1", network=NetMode.OFF, max_lifetime_s=600)
    assert spec.os == OSFamily.LINUX
    assert spec.template.kind == "vhdx"
    await target.cleanup()
    assert await target.provider.list({}) == []
