"""The conformance target wiring for the fake libvirt host."""

from __future__ import annotations

from evalspleef_sbx_libvirt.conformance import build_targets


async def test_libvirt_fake_target() -> None:
    targets = build_targets()
    target = targets[0]
    try:
        assert [item.name for item in targets] == ["libvirt-fake"]
        assert target.kind == "sandbox"
        assert target.fake is True
        assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
        assert target.cleanup is not None
        assert target.spec_factory is not None
        assert "not verified on a real libvirt host" in target.notes
        async with target.open_sandbox("conf-target") as sandbox:
            result = await sandbox.exec(["true"])
            assert result.exit_code == 0
    finally:
        await target.cleanup()
