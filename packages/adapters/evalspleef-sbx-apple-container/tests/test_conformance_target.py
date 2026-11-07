"""The conformance target wiring for the fake container CLI."""

from __future__ import annotations

from evalspleef_sbx_apple_container.conformance import build_targets

from evalspleef.spi.sandbox import Sandbox


async def test_fake_target_wiring() -> None:
    targets = build_targets()
    assert [target.name for target in targets] == ["apple-container-fake"]
    target = targets[0]
    try:
        assert target.kind == "sandbox"
        assert target.fake is True
        assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
        assert target.cleanup is not None
        assert target.spec_factory is not None
        assert target.reachable_from_sandbox == "127.0.0.1"
        async with target.open_sandbox("target-1") as sandbox:
            assert isinstance(sandbox, Sandbox)
            result = await sandbox.exec(["printf", "target-ok"])
            assert result.stdout == b"target-ok"
    finally:
        assert target.cleanup is not None
        await target.cleanup()


async def test_fault_injectors() -> None:
    targets = build_targets()
    target = targets[0]
    try:
        lose = target.injectors["lose_sandbox"]
        expire = target.injectors["expire_lifetime"]
        assert expire is not None and lose is not None
        async with target.open_sandbox("target-2") as sandbox:
            await expire()
            assert sandbox.id
    finally:
        assert target.cleanup is not None
        await target.cleanup()
