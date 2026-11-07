"""Provider contract: capabilities, object bodies, labels, quota and reaping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from evalspleef_sbx_k8s_agent_sandbox.fake import FakeKubeApi
from evalspleef_sbx_k8s_agent_sandbox.kube import (
    API_VERSION,
    EXTENSIONS_GROUP,
    SANDBOX_CLAIM_PLURAL,
    SANDBOX_GROUP,
    SANDBOX_PLURAL,
    SANDBOX_TEMPLATE_PLURAL,
    SANDBOX_WARMPOOL_PLURAL,
)
from evalspleef_sbx_k8s_agent_sandbox.network import CILIUM_GROUP, CILIUM_PLURAL, CILIUM_VERSION
from evalspleef_sbx_k8s_agent_sandbox.provider import K8sAgentSandboxProvider

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
)
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)

_TEMPLATE_REF = "ghcr.io/evalspleef/test-sandbox:latest"
_TEMPLATE_DIGEST = "sha256:" + "0" * 64


class EmptySecrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_spec(
    trial_id: str,
    *,
    network: NetMode = NetMode.OFF,
    max_lifetime_s: int = 600,
    **overrides: Any,
) -> SandboxSpec:
    spec = SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.ARM64,
        template=TemplateRef(kind="oci", ref=_TEMPLATE_REF, digest=_TEMPLATE_DIGEST),
        resources=Resources(cpus=2, memory_mb=4096, disk_mb=10240),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
        labels={
            "batch": "adapter-test",
            "trial": trial_id,
            "lease": f"lease-{trial_id}",
            "provider": "adapter-test",
        },
    )
    return replace(spec, **overrides)


async def _get(
    api: FakeKubeApi,
    group: str,
    plural: str,
    name: str,
    version: str = API_VERSION,
) -> dict[str, Any] | None:
    return await api.get_object(
        group=group,
        version=version,
        plural=plural,
        namespace="spleef-test",
        name=name,
    )


def test_gvisor_and_kata_declare_the_expected_isolation() -> None:
    gvisor = K8sAgentSandboxProvider(
        "g", {"runtime_class": "runsc"}, EmptySecrets(), api=FakeKubeApi()
    )
    kata = K8sAgentSandboxProvider(
        "k", {"runtime_class": "kata-qemu"}, EmptySecrets(), api=FakeKubeApi()
    )
    assert gvisor.capabilities().isolation is Isolation.L2
    assert kata.capabilities().isolation is Isolation.L3
    assert gvisor.isolation_profile().declared_isolation is Isolation.L2
    assert kata.isolation_profile().declared_isolation is Isolation.L3


def test_unknown_runtime_class_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported runtime class"):
        K8sAgentSandboxProvider("bad", {"runtime_class": "runc"}, EmptySecrets())


def test_capabilities_are_exact() -> None:
    provider = K8sAgentSandboxProvider("caps", {}, EmptySecrets(), api=FakeKubeApi())
    caps = provider.capabilities()
    assert caps.os_families == frozenset({OSFamily.LINUX})
    assert caps.arches == frozenset({Arch.X86_64, Arch.ARM64})
    assert caps.net_modes == frozenset(
        {NetMode.OFF, NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR, NetMode.FULL}
    )
    assert caps.allowlist_kinds == frozenset({"hostname"})
    assert caps.enforces_limits == frozenset({"cpu", "memory"})
    assert caps.snapshot == "none"
    assert caps.dynamic_network_policy is False
    assert caps.host_mounts is False


async def test_create_writes_the_sandbox_and_policy_objects(
    provider: K8sAgentSandboxProvider, api: FakeKubeApi
) -> None:
    sandbox = await provider.create(build_spec("create-1"))
    body = await _get(api, SANDBOX_GROUP, SANDBOX_PLURAL, sandbox.id)
    assert body is not None
    metadata = body["metadata"]
    labels = metadata["labels"]
    assert labels["batch"] == "adapter-test"
    assert labels["trial"] == "create-1"
    assert labels["lease"] == "lease-create-1"
    assert labels["spleef.provider"] == "adapter-test"
    assert int(labels["spleef.expires_at"]) > int(labels["spleef.created_at"])

    spec = body["spec"]
    assert spec["shutdownPolicy"] == "Delete"
    assert spec["shutdownTime"].endswith("Z")
    pod = spec["podTemplate"]["spec"]
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["hostname"] == "sandbox"
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64"}
    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/evalspleef/test-sandbox:latest"
    assert container["workingDir"] == "/workspace"
    assert container["resources"]["limits"] == {
        "cpu": "2",
        "memory": "4096Mi",
        "ephemeral-storage": "10240Mi",
    }
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["runAsNonRoot"] is True

    policy = await _get(
        api, CILIUM_GROUP, CILIUM_PLURAL, f"{sandbox.id}-egress", version=CILIUM_VERSION
    )
    assert policy is not None
    assert policy["spec"]["egress"] == []

    await sandbox.destroy()


async def test_real_digest_is_appended_to_the_image(provider: K8sAgentSandboxProvider) -> None:
    digest = "sha256:" + "ab" * 32
    spec = build_spec(
        "digest-1",
        template=TemplateRef(
            kind="oci",
            ref="ghcr.io/evalspleef/test-sandbox:latest",
            digest=digest,
        ),
    )
    sandbox = await provider.create(spec)
    try:
        body = await provider.api.get_object(
            group=SANDBOX_GROUP,
            version=API_VERSION,
            plural=SANDBOX_PLURAL,
            namespace="spleef-test",
            name=sandbox.id,
        )
        assert body is not None
        image = body["spec"]["podTemplate"]["spec"]["containers"][0]["image"]
        assert image.endswith("@" + digest)
    finally:
        await sandbox.destroy()


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"os": OSFamily.MACOS}, CapabilityNotSupported),
        ({"gui": True}, CapabilityNotSupported),
        ({"resources": replace(build_spec("x").resources, gpus=1)}, CapabilityNotSupported),
        (
            {"template": TemplateRef(kind="e2b_template", ref="t", digest="sha256:" + "0" * 64)},
            CapabilityNotSupported,
        ),
        ({"max_lifetime_s": 7201}, CapabilityNotSupported),
        (
            {"network": NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",))},
            PolicyNotEnforceable,
        ),
        (
            {"network": NetworkPolicy(mode=NetMode.OFF, allow_hosts=("example.com",))},
            PolicyNotEnforceable,
        ),
        (
            {
                "network": NetworkPolicy(
                    mode=NetMode.DOCS_WHITELIST,
                    allow_hosts=("example.com",),
                    via_proxy="10.0.0.9:3128",
                )
            },
            PolicyNotEnforceable,
        ),
        (
            {"network": NetworkPolicy(mode=NetMode.DOCS_WHITELIST, allow_hosts=("bad host",))},
            PolicyNotEnforceable,
        ),
        ({"labels": {"note": "bad value"}}, SandboxCreateError),
    ],
)
async def test_unsupported_specs_are_rejected(
    provider: K8sAgentSandboxProvider,
    overrides: Mapping[str, Any],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        await provider.create(replace(build_spec("invalid"), **dict(overrides)))


async def test_quota_is_enforced(api: FakeKubeApi, clock: Clock) -> None:
    provider = K8sAgentSandboxProvider(
        "quota",
        {"max_concurrency": 1, "ready_poll_s": 0.01},
        EmptySecrets(),
        api=api,
        clock=clock,
    )
    first = await provider.create(build_spec("quota-1"))
    try:
        with pytest.raises(QuotaExceeded):
            await provider.create(build_spec("quota-2"))
    finally:
        await first.destroy()
    second = await provider.create(build_spec("quota-3"))
    await second.destroy()


async def test_fail_next_creates_then_retry_succeeds(provider: K8sAgentSandboxProvider) -> None:
    provider.fail_next_creates(1, SandboxCreateError)
    with pytest.raises(SandboxCreateError):
        await provider.create(build_spec("fail-1"))
    sandbox = await provider.create(build_spec("fail-2"))
    assert (await sandbox.exec(["true"])).exit_code == 0
    await sandbox.destroy()


async def test_list_and_reap_work_on_labels(
    provider: K8sAgentSandboxProvider, clock: Clock
) -> None:
    first = await provider.create(build_spec("reap-1"))
    second = await provider.create(build_spec("reap-2"))
    listed = await provider.list({"batch": "adapter-test"})
    assert {entry["id"] for entry in listed} == {first.id, second.id}

    clock.advance(50)
    assert await provider.reap({"batch": "adapter-test"}, older_than_s=100) == 0

    clock.advance(100)
    assert await provider.reap({"batch": "adapter-test"}, older_than_s=100) == 2
    assert await provider.list({"batch": "adapter-test"}) == []


async def test_reap_also_honours_the_lifetime_label(
    provider: K8sAgentSandboxProvider, clock: Clock
) -> None:
    sandbox = await provider.create(build_spec("expired-1", max_lifetime_s=30))
    clock.advance(31)
    assert await provider.reap({"trial": "expired-1"}, older_than_s=10_000) == 1
    assert await provider.list({"trial": "expired-1"}) == []
    del sandbox


async def test_preflight_reports_live_sandboxes(provider: K8sAgentSandboxProvider) -> None:
    sandbox = await provider.create(build_spec("preflight-1"))
    try:
        facts = await provider.preflight()
        assert facts["namespace"] == "spleef-test"
        assert facts["runtime_class"] == "gvisor"
        assert facts["live_sandboxes"] == 1
    finally:
        await sandbox.destroy()


async def test_warm_pool_is_claimed_and_never_returned(api: FakeKubeApi, clock: Clock) -> None:
    for plural, body in [
        (
            SANDBOX_TEMPLATE_PLURAL,
            {
                "apiVersion": f"{EXTENSIONS_GROUP}/{API_VERSION}",
                "kind": "SandboxTemplate",
                "metadata": {"name": "warm-template"},
                "spec": {
                    "podTemplate": {
                        "spec": {
                            "runtimeClassName": "gvisor",
                            "containers": [{"name": "workspace", "image": "warm-image"}],
                        }
                    }
                },
            },
        ),
        (
            SANDBOX_WARMPOOL_PLURAL,
            {
                "apiVersion": f"{EXTENSIONS_GROUP}/{API_VERSION}",
                "kind": "SandboxWarmPool",
                "metadata": {"name": "warm-pool"},
                "spec": {"replicas": 1, "sandboxTemplateRef": {"name": "warm-template"}},
            },
        ),
    ]:
        await api.create_object(
            group=EXTENSIONS_GROUP,
            version=API_VERSION,
            plural=plural,
            namespace="spleef-test",
            body=body,
        )

    provider = K8sAgentSandboxProvider(
        "warm",
        {
            "namespace": "spleef-test",
            "warm_pool": "warm-pool",
            "ready_timeout_s": 1.0,
            "ready_poll_s": 0.01,
        },
        EmptySecrets(),
        api=api,
        clock=clock,
    )
    sandbox = await provider.create(build_spec("warm-1"))
    assert sandbox.claimed is True
    claim = await _get(api, EXTENSIONS_GROUP, SANDBOX_CLAIM_PLURAL, sandbox.id)
    assert claim is not None
    assert claim["spec"]["warmPoolRef"] == {"name": "warm-pool"}
    assert claim["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    body = await _get(api, SANDBOX_GROUP, SANDBOX_PLURAL, sandbox.id)
    assert body is not None
    assert body["spec"]["podTemplate"]["spec"]["containers"][0]["image"] == "warm-image"
    assert body["metadata"]["annotations"]["spleef.io/claimed-from"] == "warm-pool"

    await sandbox.destroy()
    assert await _get(api, EXTENSIONS_GROUP, SANDBOX_CLAIM_PLURAL, sandbox.id) is None
    assert await _get(api, SANDBOX_GROUP, SANDBOX_PLURAL, sandbox.id) is None
    assert await _get(api, EXTENSIONS_GROUP, SANDBOX_WARMPOOL_PLURAL, "warm-pool") is not None
