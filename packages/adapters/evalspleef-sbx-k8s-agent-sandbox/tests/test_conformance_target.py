"""The conformance target wiring (fake transport only)."""

from __future__ import annotations

from evalspleef_sbx_k8s_agent_sandbox.conformance import build_targets

from evalspleef.spi.caps import NetMode


def test_target_identity_and_injectors() -> None:
    targets = build_targets()
    assert [target.name for target in targets] == ["k8s-agent-sandbox-fake"]
    target = targets[0]
    assert target.kind == "sandbox"
    assert target.fake is True
    assert set(target.injectors) == {"lose_sandbox", "expire_lifetime", "fail_create"}
    assert target.cleanup is not None
    assert target.spec_factory is not None
    assert getattr(target, "reachable_from_sandbox", None) is None


def test_spec_factory_uses_an_oci_template_and_contract_labels() -> None:
    target = build_targets()[0]
    assert target.spec_factory is not None
    spec = target.spec_factory("t", network=NetMode.OFF, max_lifetime_s=600)
    assert spec.template.kind == "oci"
    assert spec.network.mode is NetMode.OFF
    assert {"batch", "trial", "lease", "provider"} <= set(spec.labels)


def test_provider_is_bound_to_the_fake_transport() -> None:
    target = build_targets()[0]
    provider = target.provider
    assert provider.name == "k8s-agent-sandbox"
    assert provider.capabilities().allowlist_kinds == frozenset({"hostname"})
    assert provider.capabilities().enforces_limits == frozenset()
