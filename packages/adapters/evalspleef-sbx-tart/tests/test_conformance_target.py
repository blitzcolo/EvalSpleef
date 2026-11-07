"""The conformance target wiring for the fake tart CLI."""

from __future__ import annotations

from evalspleef_sbx_tart.conformance import build_targets

from evalspleef.spi.caps import NetMode, OSFamily


async def test_tart_fake_target_is_wired_for_the_kit() -> None:
    targets = build_targets()
    assert [target.name for target in targets] == ["tart-fake"]
    target = targets[0]
    try:
        assert target.kind == "sandbox"
        assert target.fake is True
        assert target.cleanup is not None
        assert target.spec_factory is not None
        assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
        assert target.provider.capabilities().gui == "vnc"
        spec = target.spec_factory("trial-1", network=NetMode.OFF, max_lifetime_s=600)
    finally:
        await target.cleanup()
    assert spec.os is OSFamily.MACOS
    assert spec.gui is True
    assert spec.labels["batch"] == "conformance"
