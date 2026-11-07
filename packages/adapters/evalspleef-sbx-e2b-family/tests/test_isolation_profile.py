"""The shipped isolation_profile.yaml stays in sync with the provider object."""

from __future__ import annotations

from pathlib import Path

import yaml
from evalspleef_sbx_e2b_family import novita as novita_module

from evalspleef.spi.caps import Isolation

PROFILE_PATH = Path(__file__).resolve().parent.parent / "isolation_profile.yaml"


def test_yaml_profile_parses_and_matches_provider_profile() -> None:
    data = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    assert data["provider"] == "novita-sandbox"
    assert data["declared_isolation"] == "L3"
    assert data["verified_isolation"] is None

    provider = novita_module.NovitaSandboxProvider("novita-test", {}, {})
    profile = provider.isolation_profile()
    assert profile.provider == data["provider"]
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    assert set(profile.invariants) == set(data["invariants"])

    for key, entry in profile.invariants.items():
        yaml_entry = data["invariants"][key]
        assert entry.waived == bool(yaml_entry.get("how") == "waived" or yaml_entry.get("waived"))
        if entry.waived:
            assert entry.reason, f"waiver {key} records no reason"
            assert entry.compensating, f"waiver {key} records no compensating control"
            assert entry.approved_by, f"waiver {key} records no approver"
            assert entry.expires, f"waiver {key} records no expiry"


def test_provider_declares_l3_and_no_verified_isolation() -> None:
    provider = novita_module.NovitaSandboxProvider("novita-test", {}, {})
    profile = provider.isolation_profile()
    assert profile.declared_isolation == Isolation.L3
    assert profile.verified_isolation is None
    # Waivers must carry the compensating controls required by section 7.7.
    waivers = profile.waivers()
    assert waivers, "expected at least the I2 waiver"
    for key, entry in waivers.items():
        assert entry.compensating and entry.expires, key
