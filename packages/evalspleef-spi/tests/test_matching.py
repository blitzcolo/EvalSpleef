from __future__ import annotations

import pytest

from evalspleef.spi import (
    Arch,
    Isolation,
    ModelCapabilities,
    ModelRequirements,
    NetMode,
    OSFamily,
    Resources,
    RuntimeRequirements,
    SandboxCapabilities,
    admit_model,
    match_requirements,
)


def make_caps(**overrides: object) -> SandboxCapabilities:
    base: dict[str, object] = {
        "os_families": frozenset({OSFamily.LINUX}),
        "arches": frozenset({Arch.X86_64, Arch.ARM64}),
        "isolation": Isolation.L1,
        "net_modes": frozenset({NetMode.OFF, NetMode.FULL}),
        "allowlist_kinds": frozenset(),
        "dynamic_network_policy": False,
        "streaming": True,
        "stdin": True,
        "snapshot": "none",
        "snapshot_fanout": False,
        "pause_resume": False,
        "gui": "none",
        "gpu": False,
        "port_expose": False,
        "host_mounts": False,
        "enforces_limits": frozenset({"cpu", "memory"}),
        "max_concurrency": None,
        "max_lifetime_s": 10_800,
        "max_resources": Resources(cpus=8, memory_mb=8192, disk_mb=20480),
        "typical_start_s": 2.0,
        "reset_cost_s": 2.0,
    }
    base.update(overrides)
    return SandboxCapabilities(**base)  # type: ignore[arg-type]


def make_req(**overrides: object) -> RuntimeRequirements:
    base: dict[str, object] = {
        "os": OSFamily.LINUX,
        "arch": frozenset({Arch.X86_64}),
        "min_isolation": Isolation.L1,
        "network": NetMode.OFF,
    }
    base.update(overrides)
    return RuntimeRequirements(**base)  # type: ignore[arg-type]


def test_matching_requirements_return_no_reasons() -> None:
    assert match_requirements(make_req(), make_caps(), egress_proxy_available=False) == []


@pytest.mark.parametrize(
    ("req_overrides", "cap_overrides", "reason"),
    [
        ({"os": OSFamily.MACOS}, {}, "os:macos"),
        ({"arch": frozenset({Arch.ARM64})}, {"arches": frozenset({Arch.X86_64})}, "arch"),
        ({"min_isolation": Isolation.L3}, {}, "isolation:L1<L3"),
        ({"network": NetMode.PACKAGE_MIRROR}, {}, "net:package_mirror"),
        ({"gpu": True}, {}, "gpu"),
        ({"gui": True}, {}, "gui"),
        ({"max_lifetime_s": 20_000}, {}, "lifetime"),
        ({"memory_mb": 16_384}, {}, "resources"),
    ],
)
def test_each_unmet_requirement_is_named(
    req_overrides: dict[str, object], cap_overrides: dict[str, object], reason: str
) -> None:
    reasons = match_requirements(
        make_req(**req_overrides), make_caps(**cap_overrides), egress_proxy_available=False
    )
    assert reason in reasons


def test_hostname_allowlist_needs_native_support_or_a_proxy() -> None:
    req = make_req(network=NetMode.DOCS_WHITELIST)
    caps = make_caps(net_modes=frozenset({NetMode.OFF, NetMode.DOCS_WHITELIST}))
    assert match_requirements(req, caps, egress_proxy_available=False) == ["net:hostname-allowlist"]
    assert match_requirements(req, caps, egress_proxy_available=True) == []
    native = make_caps(
        net_modes=frozenset({NetMode.DOCS_WHITELIST}), allowlist_kinds=frozenset({"hostname"})
    )
    assert match_requirements(req, native, egress_proxy_available=False) == []


def test_model_admission_names_the_missing_capability() -> None:
    caps = ModelCapabilities(tool_calling="none", context_tokens_effective=32_000)
    req = ModelRequirements(tool_calling="required", min_context_tokens=64_000)
    assert admit_model(req, caps) == [
        "capability:tool_calling",
        "capability:context_tokens_effective",
    ]
    capable = ModelCapabilities(tool_calling="parallel", context_tokens_effective=128_000)
    assert admit_model(req, capable) == []
    assert admit_model(ModelRequirements(tool_calling="optional"), caps) == []
