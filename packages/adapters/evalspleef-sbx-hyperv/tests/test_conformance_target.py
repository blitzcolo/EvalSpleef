"""The conformance target wiring for the fake Hyper-V host and fake guests."""

from __future__ import annotations

import socket

from evalspleef_sbx_hyperv.conformance import build_targets

from evalspleef.spi.caps import Arch, NetMode, OSFamily


def _connects(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5.0):
            return True
    except OSError:
        return False


async def test_hyperv_fake_target_is_wired_for_the_kit() -> None:
    targets = build_targets()
    assert [target.name for target in targets] == ["hyperv-fake"]
    target = targets[0]
    try:
        assert target.kind == "sandbox"
        assert target.fake is True
        assert target.cleanup is not None
        assert target.spec_factory is not None
        assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
        assert target.provider.capabilities().gui == "rdp"
        assert "FakeHostShell" in target.notes
        spec = target.spec_factory("trial-1", network=NetMode.OFF, max_lifetime_s=600)
    finally:
        await target.cleanup()
    assert spec.os is OSFamily.WINDOWS
    assert spec.arch is Arch.X86_64
    assert spec.template.kind == "vhdx"
    assert spec.gui is True
    assert spec.labels["batch"] == "conformance"
    assert spec.labels["lease"] == "lease-trial-1"


async def test_the_rdp_stub_answers_until_cleanup() -> None:
    (target,) = build_targets()
    assert target.cleanup is not None
    try:
        created = await target.provider.create(  # type: ignore[union-attr]
            target.spec_factory("rdp-1", network=NetMode.OFF, max_lifetime_s=600)  # type: ignore[misc]
        )
        try:
            endpoint = await created.gui_endpoint()
            host, port = endpoint.removeprefix("rdp://").rsplit(":", 1)
            assert _connects(host, int(port))
        finally:
            await created.destroy()
    finally:
        await target.cleanup()
    assert not _connects(host, int(port))
