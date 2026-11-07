"""Static matching at freeze: candidates, rejection reasons and skip reasons."""

from __future__ import annotations

from evalspleef.control.scheduling import StaticMatcher
from evalspleef.spi.caps import Isolation, NetMode, OSFamily


def test_matching_instances_become_candidates_in_route_order(
    make_provider, make_directory, reqs
) -> None:
    providers = {"a": make_provider("a"), "b": make_provider("b")}
    matcher = StaticMatcher(make_directory(providers, {"linux": ["b", "a"]}))

    match = matcher.match(reqs())

    assert match.matched
    assert match.agent.candidates == ("b", "a")
    assert match.verifier.candidates == ("b", "a")
    assert match.reasons == ()


def test_rejected_instance_is_recorded_and_the_other_stays_a_candidate(
    make_provider, make_directory, reqs
) -> None:
    providers = {
        "weak": make_provider("weak", isolation=Isolation.L1),
        "strong": make_provider("strong", isolation=Isolation.L3),
    }
    matcher = StaticMatcher(make_directory(providers, {"linux": ["weak", "strong"]}))

    match = matcher.match(reqs(min_isolation=Isolation.L3))

    assert match.agent.candidates == ("strong",)
    assert match.agent.rejections == {"weak": ("isolation:L1<L3",)}
    assert match.matched


def test_unmatched_cell_reports_every_reason(make_provider, make_directory, reqs) -> None:
    providers = {"a": make_provider("a", gpu=False)}
    matcher = StaticMatcher(make_directory(providers, {"linux": ["a"]}))

    match = matcher.match(reqs(gpu=True, min_isolation=Isolation.L4))

    assert not match.matched
    assert match.reasons == ("isolation:L1<L4", "gpu")


def test_no_route_for_the_os_is_its_own_reason(make_provider, make_directory, reqs) -> None:
    matcher = StaticMatcher(make_directory({"a": make_provider("a")}, {"linux": ["a"]}))

    match = matcher.match(reqs(os=OSFamily.WINDOWS))

    assert not match.matched
    assert match.reasons == ("route:none",)


def test_hostname_allowlist_is_satisfied_by_an_egress_proxy(
    make_provider, make_directory, reqs
) -> None:
    providers = {"a": make_provider("a", allowlist_kinds=frozenset())}
    requirements = reqs(network=NetMode.DOCS_WHITELIST, allow_hosts=("docs.python.org",))

    without_proxy = StaticMatcher(make_directory(providers, {"linux": ["a"]})).match(requirements)
    with_proxy = StaticMatcher(
        make_directory(providers, {"linux": ["a"]}, proxies=frozenset({"a"}))
    ).match(requirements)

    assert without_proxy.reasons == ("net:hostname-allowlist",)
    assert with_proxy.matched


def test_verifier_runs_with_network_off_so_a_full_only_provider_cannot_verify(
    make_provider, make_directory, reqs
) -> None:
    providers = {"full-only": make_provider("full-only", net_modes=frozenset({NetMode.FULL}))}
    matcher = StaticMatcher(make_directory(providers, {"linux": ["full-only"]}))

    match = matcher.match(reqs(network=NetMode.FULL))

    assert match.agent.candidates == ("full-only",)
    assert match.verifier.candidates == ()
    assert not match.matched
    assert match.reasons == ("net:off",)
