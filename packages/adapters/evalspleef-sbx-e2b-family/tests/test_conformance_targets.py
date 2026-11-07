"""The conformance targets: fake-backed providers with fault injectors."""

from __future__ import annotations

import pytest
from evalspleef_sbx_e2b_family import novita as novita_module
from evalspleef_sbx_e2b_family import selfhost as selfhost_module
from evalspleef_sbx_e2b_family.conformance import build_targets

from evalspleef.spi.caps import NetMode
from evalspleef.spi.errors import SandboxCreateError, SandboxLost


@pytest.fixture(scope="module")
def targets():
    built = build_targets()
    yield built
    for target in built:
        if target.cleanup is not None:
            import asyncio

            asyncio.run(target.cleanup())


def test_two_fake_targets(targets) -> None:
    assert [target.name for target in targets] == ["novita-fake", "e2b-selfhost-fake"]
    for target in targets:
        assert target.kind == "sandbox"
        assert target.fake is True
        assert target.spec_factory is not None
        assert set(target.injectors) == {"lose_sandbox", "fail_create", "expire_lifetime"}
        assert "not verified" in target.notes


def test_targets_patch_their_adapter_modules(targets) -> None:
    novita_target, selfhost_target = targets
    assert isinstance(novita_target.provider, novita_module.NovitaSandboxProvider)
    assert isinstance(selfhost_target.provider, selfhost_module.E2BSelfHostProvider)
    # The SDK entry points were replaced by the fake while the target lives.
    assert novita_module.Sandbox.__name__ == "ClusterSandbox"
    assert selfhost_module.Sandbox.__name__ == "ClusterSandbox"


async def test_open_sandbox_lifecycle(targets) -> None:
    target = targets[0]
    async with target.open_sandbox("conf-t-1") as sandbox:
        result = await sandbox.exec(["echo", "ok"])
        assert result.exit_code == 0
        assert result.stdout.strip() == b"ok"


async def test_injectors_lose_fail_expire(targets) -> None:
    target = targets[0]
    async with target.open_sandbox("conf-t-2") as sandbox:
        await target.injectors["lose_sandbox"](sandbox)
        with pytest.raises(SandboxLost):
            await sandbox.exec(["true"])

    await target.injectors["fail_create"]()
    provider = target.provider
    spec = target.spec_factory("conf-t-3", network=NetMode.OFF, max_lifetime_s=600)
    with pytest.raises(SandboxCreateError):
        await provider.create(spec)
    retry_spec = target.spec_factory("conf-t-4", network=NetMode.OFF, max_lifetime_s=600)
    sandbox = await provider.create(retry_spec)
    await sandbox.destroy()

    async with target.open_sandbox("conf-t-5") as sandbox:
        await target.injectors["expire_lifetime"]()
        with pytest.raises(SandboxLost):
            await sandbox.exec(["true"])


async def test_targets_are_isolated_from_each_other(targets) -> None:
    """Each fake target talks to its own cluster through its patched module."""
    first, second = targets
    async with first.open_sandbox("iso-a") as s0, second.open_sandbox("iso-b") as s1:
        assert s0.id != s1.id
        listed_first = {entry["id"] for entry in await first.provider.list({})}
        listed_second = {entry["id"] for entry in await second.provider.list({})}
        assert listed_first == {s0.id}
        assert listed_second == {s1.id}
