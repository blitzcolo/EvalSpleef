from __future__ import annotations

from typing import Any

import yaml

from evalspleef.egress.configgen import CONFIG_CAVEAT, envoy_yaml, squid_conf
from evalspleef.egress.policy import Policy
from evalspleef.spi import NetMode

ALLOWED = ("developer.huawei.com", "*.example.com")


def _policy(**overrides: Any) -> Policy:
    values: dict[str, Any] = {
        "mode": NetMode.PACKAGE_MIRROR,
        "allow": ALLOWED,
        "block": ("*.internal", "leaderboard.example.net"),
    }
    values.update(overrides)
    return Policy(**values)


def _filter_chain(config: dict[str, Any]) -> list[dict[str, Any]]:
    listener = config["static_resources"]["listeners"][0]
    return listener["filter_chains"][0]["filters"]


def _http_filters(config: dict[str, Any]) -> list[dict[str, Any]]:
    network_filters = _filter_chain(config)
    hcm = next(
        item
        for item in network_filters
        if item["name"] == "envoy.filters.network.http_connection_manager"
    )
    return hcm["typed_config"]["http_filters"]


def _sni_patterns(rbac_filter: dict[str, Any], policy_name: str) -> list[str]:
    rules = rbac_filter["typed_config"]["rules"]
    principals = rules["policies"][policy_name]["principals"]
    patterns: list[str] = []
    for principal in principals:
        for identity in principal["or_ids"]["ids"]:
            if "sni_dns_match" in identity:
                patterns.append(identity["sni_dns_match"]["pattern"])
    return patterns


def test_envoy_yaml_emits_a_rule_for_every_allowed_host() -> None:
    policy = _policy()
    text = envoy_yaml(policy)
    assert "not run against a real Squid or Envoy" in text

    config = yaml.safe_load(text)
    filters = _http_filters(config)
    names = [item["name"] for item in filters]
    assert "envoy.filters.http.sni_dynamic_forward_proxy" in names
    assert "envoy.filters.http.dynamic_forward_proxy" in names
    assert names.index("envoy.filters.http.sni_dynamic_forward_proxy") < names.index(
        "envoy.filters.http.rbac"
    )

    allow_filter = next(
        item for item in filters if item["typed_config"].get("stat_prefix") == "egress_allow_rbac"
    )
    patterns = _sni_patterns(allow_filter, "allowlist")
    for host in policy.allow_rules:
        assert host[0] in patterns

    permissions = allow_filter["typed_config"]["rules"]["policies"]["allowlist"]["permissions"]
    regex = permissions[0]["and_rules"]["rules"][0]["header"]["string_match"]["safe_regex"]["regex"]
    assert regex == "^(GET|HEAD|CONNECT)$"

    block_filter = next(
        item for item in filters if item["typed_config"].get("stat_prefix") == "egress_block_rbac"
    )
    blocked_patterns = _sni_patterns(block_filter, "blocklist")
    for pattern in policy.block_rules:
        assert pattern in blocked_patterns


def test_envoy_yaml_blocks_metadata_cidrs_at_the_network_layer() -> None:
    config = yaml.safe_load(envoy_yaml(_policy()))
    network_filters = _filter_chain(config)
    rbac = network_filters[0]
    assert rbac["name"] == "envoy.filters.network.rbac"
    principals = rbac["typed_config"]["rules"]["policies"]["blocked_destinations"]["principals"]
    assert {
        "destination_ip": {
            "address_prefix": "169.254.169.254",
            "prefix_len": 32,
        }
    } in principals
    assert any(
        principal["destination_ip"]["address_prefix"] == "169.254.0.0" for principal in principals
    )


def test_envoy_off_and_full_modes_use_deny_all_and_allow_all() -> None:
    off_filters = _http_filters(yaml.safe_load(envoy_yaml(Policy(mode=NetMode.OFF))))
    off_rbac = next(
        item
        for item in off_filters
        if item["typed_config"].get("stat_prefix") == "egress_allow_rbac"
    )
    assert off_rbac["typed_config"]["rules"]["action"] == "DENY"

    full_filters = _http_filters(yaml.safe_load(envoy_yaml(Policy(mode=NetMode.FULL))))
    full_rbac = next(
        item
        for item in full_filters
        if item["typed_config"].get("stat_prefix") == "egress_allow_rbac"
    )
    assert full_rbac["typed_config"]["rules"]["action"] == "ALLOW"
    assert "allow_all" in full_rbac["typed_config"]["rules"]["policies"]


def test_squid_conf_emits_acl_rules_for_every_allowed_host() -> None:
    policy = _policy()
    text = squid_conf(policy)
    assert CONFIG_CAVEAT.strip() in text
    assert "not run against a real Squid or Envoy" in text
    assert "acl allowed_hosts dstdomain developer.huawei.com .example.com" in text
    assert "acl allowed_servers ssl::server_name developer.huawei.com .example.com" in text
    assert "ssl_bump peek all" in text
    assert "ssl_bump splice allowed_servers" in text
    assert "acl blocked_hosts dstdomain .internal leaderboard.example.net" in text
    assert "acl blocked_metadata dst 169.254.169.254/32 169.254.0.0/16" in text
    assert "acl read_only_methods method GET HEAD CONNECT" in text
    assert "http_access deny !read_only_methods" in text


def test_squid_off_and_full_modes() -> None:
    off_text = squid_conf(Policy(mode=NetMode.OFF))
    assert "http_access deny all" in off_text
    assert "acl allowed_hosts" not in off_text

    full_text = squid_conf(Policy(mode=NetMode.FULL))
    assert "http_access allow all" in full_text
