"""Cilium egress policy generation (no cluster required)."""

from __future__ import annotations

from evalspleef_sbx_k8s_agent_sandbox.network import build_egress_policy, policy_name

from evalspleef.spi.caps import NetMode
from evalspleef.spi.sandbox import NetworkPolicy

_LABELS = {"spleef.provider": "adapter-test", "lease": "lease-1"}


def _policy(mode: NetMode, hosts: tuple[str, ...] = ()) -> dict | None:
    return build_egress_policy(
        sandbox_name="spleef-abc",
        namespace="spleef-test",
        pod_labels=_LABELS,
        policy=NetworkPolicy(mode=mode, allow_hosts=hosts),
    )


def test_off_generates_default_deny_egress() -> None:
    body = _policy(NetMode.OFF)
    assert body is not None
    assert body["apiVersion"] == "cilium.io/v2"
    assert body["kind"] == "CiliumNetworkPolicy"
    assert body["metadata"]["name"] == policy_name("spleef-abc")
    assert body["metadata"]["namespace"] == "spleef-test"
    assert body["spec"]["endpointSelector"]["matchLabels"] == _LABELS
    assert body["spec"]["egress"] == []


def test_full_generates_no_policy() -> None:
    assert _policy(NetMode.FULL) is None


def test_allowlist_generates_dns_and_fqdn_rules() -> None:
    body = _policy(NetMode.DOCS_WHITELIST, ("docs.python.org", "pypi.org"))
    assert body is not None
    rules = body["spec"]["egress"]
    assert len(rules) == 3, "one DNS rule plus one rule per allowlisted hostname"
    assert rules[0]["toEndpoints"][0]["matchLabels"]["k8s:k8s-app"] == "kube-dns"
    names = {rule["toFQDNs"][0]["matchName"] for rule in rules[1:]}
    assert names == {"docs.python.org", "pypi.org"}
    for rule in rules[1:]:
        ports = {port["port"] for port in rule["toPorts"][0]["ports"]}
        assert ports == {"80", "443"}


def test_empty_allowlist_keeps_only_dns() -> None:
    body = _policy(NetMode.PACKAGE_MIRROR)
    assert body is not None
    assert len(body["spec"]["egress"]) == 1
    assert "toFQDNs" not in body["spec"]["egress"][0]


def test_policy_records_the_mode() -> None:
    body = _policy(NetMode.OFF)
    assert body is not None
    assert body["metadata"]["annotations"]["spleef.io/network-mode"] == "off"
