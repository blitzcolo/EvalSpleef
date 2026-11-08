from __future__ import annotations

from pathlib import Path

import pytest
from evalspleef_harbor_bridge.env import (
    BaseEnvironment,
    HarborExecResult,
    SpleefEnvironment,
    find_sandbox_provider,
    map_sandbox_capabilities,
)

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxCapabilities,
    SandboxSpec,
)
from evalspleef.testing.mock_sandbox import MockSandbox, MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

DIGEST = "sha256:" + "d" * 64


@pytest.fixture
def mock_provider(tmp_path: Path) -> MockSandboxProvider:
    return MockSandboxProvider(
        instance="test-mock",
        config={"root_dir": str(tmp_path / "mock_root")},
        secrets=DictSecretResolver({}),
    )


class DynamicNetMockSandbox(MockSandbox):
    async def set_network(self, policy: NetworkPolicy) -> None:
        pass


class DynamicNetMockProvider(MockSandboxProvider):
    """MockSandboxProvider with dynamic_network_policy enabled for testing."""

    def capabilities(self) -> SandboxCapabilities:
        base = super().capabilities()
        return SandboxCapabilities(
            os_families=base.os_families,
            arches=base.arches,
            isolation=base.isolation,
            net_modes=base.net_modes,
            allowlist_kinds=base.allowlist_kinds,
            dynamic_network_policy=True,
            streaming=base.streaming,
            stdin=base.stdin,
            snapshot=base.snapshot,
            snapshot_fanout=base.snapshot_fanout,
            pause_resume=base.pause_resume,
            gui=base.gui,
            gpu=base.gpu,
            port_expose=base.port_expose,
            host_mounts=base.host_mounts,
            enforces_limits=base.enforces_limits,
            max_concurrency=base.max_concurrency,
            max_lifetime_s=base.max_lifetime_s,
            max_resources=base.max_resources,
            typical_start_s=base.typical_start_s,
            reset_cost_s=base.reset_cost_s,
        )

    async def create(self, spec: SandboxSpec) -> Sandbox:
        base_sbx = await super().create(spec)
        sbx = DynamicNetMockSandbox(
            sandbox_id=base_sbx.id,
            spec=spec,
            clock=base_sbx._clock,
            created_at=base_sbx.created_at,
            expires_at=base_sbx.expires_at,
        )
        return sbx


def test_map_sandbox_capabilities():
    caps = SandboxCapabilities(
        os_families=frozenset({OSFamily.LINUX, OSFamily.WINDOWS}),
        arches=frozenset({Arch.X86_64}),
        isolation=1,  # type: ignore[arg-type]
        net_modes=frozenset({NetMode.OFF, NetMode.FULL}),
        allowlist_kinds=frozenset({"hostname"}),
        dynamic_network_policy=True,
        streaming=True,
        stdin=True,
        snapshot="none",
        snapshot_fanout=False,
        pause_resume=False,
        gui="none",
        gpu=True,
        port_expose=False,
        host_mounts=False,
        enforces_limits=frozenset(),
        max_concurrency=10,
        max_lifetime_s=3600,
        max_resources=Resources(cpus=4, memory_mb=8192, disk_mb=20480),
        typical_start_s=0.5,
        reset_cost_s=0.1,
    )
    mapped = map_sandbox_capabilities(caps)
    assert mapped["disable_internet"] is True
    assert mapped["network_allowlist_hostnames"] is True
    assert mapped["dynamic_network_policy"] is True
    assert mapped["gpus"] is True
    assert mapped["windows"] is True
    assert mapped["stream"] is True


def test_subclass_check():
    assert issubclass(SpleefEnvironment, BaseEnvironment)


@pytest.mark.asyncio
async def test_env_lifecycle_and_exec(mock_provider: MockSandboxProvider):
    env = SpleefEnvironment(
        task_env_config={
            "image": "ubuntu:24.04",
            "digest": DIGEST,
            "cpus": 2,
            "memory_mb": 2048,
            "storage_mb": 5120,
        },
        provider=mock_provider,
    )

    assert env.type() == "evalspleef"
    caps = env.capabilities
    assert isinstance(caps, dict)
    assert "disable_internet" in caps

    await env.start()
    assert env.sandbox is not None

    # Test exec
    res: HarborExecResult = await env.exec(["echo", "hello harbor"])
    assert res.return_code == 0
    assert "hello harbor" in res.stdout
    assert res.stderr == ""

    # Test stop
    await env.stop(delete=True)
    assert env.sandbox is None


@pytest.mark.asyncio
async def test_env_file_transfer(mock_provider: MockSandboxProvider, tmp_path: Path):
    env = SpleefEnvironment(
        task_env_config={"image": "alpine:latest", "digest": DIGEST},
        provider=mock_provider,
    )
    await env.start()

    # Upload file
    local_src = tmp_path / "hello.txt"
    local_src.write_text("harbor test content", encoding="utf-8")
    remote_dst = "/workspace/hello.txt"

    await env.upload_file(local_src, remote_dst)

    # Download file back
    local_dst = tmp_path / "downloaded.txt"
    await env.download_file(remote_dst, local_dst)
    assert local_dst.read_text(encoding="utf-8") == "harbor test content"

    # Upload directory
    src_dir = tmp_path / "src_dir"
    src_dir.mkdir()
    (src_dir / "file1.txt").write_text("f1", encoding="utf-8")
    (src_dir / "file2.txt").write_text("f2", encoding="utf-8")

    remote_dir = "/workspace/remote_dir"
    await env.upload_dir(src_dir, remote_dir)

    # Download directory
    dst_dir = tmp_path / "dst_dir"
    await env.download_dir(remote_dir, dst_dir)
    assert (dst_dir / "file1.txt").read_text(encoding="utf-8") == "f1"
    assert (dst_dir / "file2.txt").read_text(encoding="utf-8") == "f2"

    await env.stop()


@pytest.mark.asyncio
async def test_env_dynamic_network_policy_supported(tmp_path: Path):
    provider = DynamicNetMockProvider(
        instance="test-dyn",
        config={"root_dir": str(tmp_path / "mock_root")},
        secrets=DictSecretResolver({}),
    )
    env = SpleefEnvironment(
        task_env_config={"image": "ubuntu:24.04", "digest": DIGEST},
        provider=provider,
    )
    await env.start()

    policy = NetworkPolicy(mode=NetMode.OFF)
    await env.set_network_policy(policy)

    await env.stop()


@pytest.mark.asyncio
async def test_env_dynamic_network_policy_unsupported(mock_provider: MockSandboxProvider):
    env = SpleefEnvironment(
        task_env_config={"image": "ubuntu:24.04", "digest": DIGEST},
        provider=mock_provider,
    )
    await env.start()

    policy = NetworkPolicy(mode=NetMode.OFF)
    with pytest.raises(CapabilityNotSupported):
        await env.set_network_policy(policy)

    await env.stop()


def test_find_sandbox_provider_not_found():
    with pytest.raises(LookupError, match="No sandbox provider entry point found"):
        find_sandbox_provider("nonexistent_provider_xyz")


def test_the_digest_comes_from_the_config_the_reference_or_the_registry(tmp_path: Path) -> None:
    from evalspleef_harbor_bridge.env import TemplateNotPinned, resolve_digest

    other = "sha256:" + "e" * 64
    assert resolve_digest("img:1", DIGEST, None) == DIGEST
    assert resolve_digest(f"img@{other}", None, None) == other
    registry = {"logic-001": {"tag": "llm-eval/logic-001:local", "image_id": other}}
    assert resolve_digest("llm-eval/logic-001:local", None, registry) == other
    for image, explicit in (("img:1", None), ("img:1", "sha256:short")):
        with pytest.raises(TemplateNotPinned):
            resolve_digest(image, explicit, registry)


@pytest.mark.asyncio
async def test_an_unpinned_task_image_is_refused(mock_provider: MockSandboxProvider) -> None:
    from evalspleef_harbor_bridge.env import TemplateNotPinned

    env = SpleefEnvironment(task_env_config={"image": "ubuntu:24.04"}, provider=mock_provider)
    with pytest.raises(TemplateNotPinned):
        await env.start()
