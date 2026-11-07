from __future__ import annotations

from pathlib import Path

import pytest

from evalspleef.egress.policy import (
    BLOCK_BLOCKED_CIDR,
    BLOCK_BLOCKED_HOST,
    BLOCK_HOST_NOT_ALLOWED,
    BLOCK_METHOD,
    BLOCK_MIRROR_CONNECT,
    BLOCK_MODE_OFF,
    DEFAULT_BLOCKED_CIDRS,
    DEFAULT_BLOCKED_HOSTS,
    MirrorRoute,
    Policy,
)
from evalspleef.spi import NetMode


def test_loads_yaml_and_merges_platform_defaults(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
mode: package_mirror
allow:
  - developer.huawei.com
  - "*.example.com"
  - https://docs.example.com/guide
block:
  - leaderboard.example.net
blocked_cidrs:
  - 10.0.0.0/8
mirrors:
  pip: http://pypi-mirror.internal:8080
  npm: http://npm-mirror.internal:4873
""",
        encoding="utf-8",
    )

    policy = Policy.from_yaml(path)

    assert policy.mode is NetMode.PACKAGE_MIRROR
    assert policy.allow == (
        "developer.huawei.com",
        "*.example.com",
        "https://docs.example.com/guide",
    )
    for host in DEFAULT_BLOCKED_HOSTS:
        assert host in policy.block
    assert "leaderboard.example.net" in policy.block
    for cidr in DEFAULT_BLOCKED_CIDRS:
        assert cidr in policy.blocked_cidrs
    assert "10.0.0.0/8" in policy.blocked_cidrs
    assert [route.registry for route in policy.mirrors] == ["pip", "npm"]
    pip = policy.mirrors[0]
    assert pip.hosts == ("pypi.org", "files.pythonhosted.org")
    assert pip.base_host == "pypi-mirror.internal"
    assert pip.base_port == 8080
    assert pip.base_tls is False


def test_unknown_keys_and_invalid_values_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown policy keys"):
        Policy.from_mapping({"mode": "off", "allowlist": ["example.com"]})
    with pytest.raises(ValueError, match=r"invalid policy\.mode"):
        Policy.from_mapping({"mode": "everything"})
    with pytest.raises(ValueError, match="invalid blocked CIDR"):
        Policy.from_mapping({"mode": "off", "blocked_cidrs": ["not-a-cidr"]})
    with pytest.raises(ValueError, match="unsupported mirror registry"):
        Policy.from_mapping({"mode": "package_mirror", "mirrors": {"maven": "http://m.internal"}})


def test_wildcard_matches_subdomains_but_not_the_apex() -> None:
    policy = Policy(mode=NetMode.DOCS_WHITELIST, allow=("*.example.com",))
    assert policy.decide(method="GET", host="a.example.com", port=443, path="/").allowed
    assert policy.decide(method="GET", host="a.b.example.com", port=443, path="/").allowed
    apex = policy.decide(method="GET", host="example.com", port=443, path="/")
    assert apex.allowed is False
    assert apex.reason == BLOCK_HOST_NOT_ALLOWED
    lookalike = policy.decide(method="GET", host="evil-example.com", port=443, path="/")
    assert lookalike.allowed is False


def test_exact_hosts_and_url_path_prefixes() -> None:
    policy = Policy(
        mode=NetMode.DOCS_WHITELIST,
        allow=("docs.example.com/guide",),
    )
    assert policy.decide(method="GET", host="docs.example.com", port=80, path="/guide").allowed
    assert policy.decide(
        method="GET", host="docs.example.com", port=80, path="/guide/intro?lang=en"
    ).allowed
    outside = policy.decide(method="GET", host="docs.example.com", port=80, path="/secrets")
    assert outside.allowed is False
    # CONNECT exposes no path; the host is matched and the limitation is documented.
    assert policy.decide(
        method="CONNECT", host="docs.example.com", port=443, path="", is_connect=True
    ).allowed


def test_off_denies_everything_and_full_allows() -> None:
    off = Policy(mode=NetMode.OFF, allow=("example.com",))
    denied = off.decide(method="GET", host="example.com", port=443, path="/")
    assert denied.allowed is False
    assert denied.reason == BLOCK_MODE_OFF

    full = Policy(mode=NetMode.FULL)
    allowed = full.decide(method="POST", host="example.com", port=443, path="/api")
    assert allowed.allowed is True
    assert allowed.route is not None
    assert allowed.route.host == "example.com"


def test_blocklist_and_metadata_cidrs_win_over_allowlist() -> None:
    policy = Policy(
        mode=NetMode.FULL,
        allow=("secret.example.com",),
        block=("secret.example.com",),
    )
    blocked = policy.decide(method="GET", host="secret.example.com", port=443, path="/")
    assert blocked.allowed is False
    assert blocked.reason == BLOCK_BLOCKED_HOST

    metadata = policy.decide(method="GET", host="169.254.169.254", port=80, path="/latest")
    assert metadata.allowed is False
    assert metadata.reason == BLOCK_BLOCKED_CIDR
    link_local = policy.decide(method="GET", host="169.254.1.1", port=80, path="/")
    assert link_local.allowed is False
    assert link_local.reason == BLOCK_BLOCKED_CIDR


def test_read_only_modes_reject_mutating_methods() -> None:
    policy = Policy(mode=NetMode.PACKAGE_MIRROR, allow=("docs.example.com",))
    post = policy.decide(method="POST", host="docs.example.com", port=80, path="/upload")
    assert post.allowed is False
    assert post.reason == BLOCK_METHOD
    assert policy.decide(method="HEAD", host="docs.example.com", port=80, path="/").allowed


def test_package_mirror_rewrites_registry_requests() -> None:
    policy = Policy(
        mode=NetMode.PACKAGE_MIRROR,
        mirrors=(
            MirrorRoute(
                "cargo",
                ("crates.io", "static.crates.io"),
                "http://cargo-mirror.internal:8081",
            ),
        ),
    )
    decision = policy.decide(
        method="GET", host="crates.io", port=80, path="/api/v1/crates/serde?x=1"
    )
    assert decision.allowed is True
    assert decision.route is not None
    assert decision.route.host == "cargo-mirror.internal"
    assert decision.route.port == 8081
    assert decision.route.path == "/api/v1/crates/serde?x=1"

    connect = policy.decide(method="CONNECT", host="crates.io", port=443, path="", is_connect=True)
    assert connect.allowed is False
    assert connect.reason == BLOCK_MIRROR_CONNECT

    direct = policy.decide(method="GET", host="cargo-mirror.internal", port=8081, path="/index")
    assert direct.allowed is True


def test_mirror_path_prefix_is_prepended() -> None:
    route = MirrorRoute("ohpm", ("ohpm.openharmony.cn",), "https://ohpm-mirror.internal/ohpm/")
    assert route.base_path == "/ohpm"
    assert route.base_tls is True
    assert route.base_port == 443
    assert route.rewrite_path("/registry/foo") == "/ohpm/registry/foo"


def test_blank_host_is_invalid() -> None:
    policy = Policy(mode=NetMode.FULL)
    decision = policy.decide(method="GET", host="", port=80, path="/")
    assert decision.allowed is False
