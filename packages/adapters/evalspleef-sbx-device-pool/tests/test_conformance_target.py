"""Unit tests for conformance targets wiring."""

from __future__ import annotations

from evalspleef_sbx_device_pool.conformance import build_targets


def test_build_conformance_targets() -> None:
    targets = build_targets()
    names = [t.name for t in targets]
    assert "device-pool-fake" in names
    assert "perf-host-fake" in names

    dev = next(t for t in targets if t.name == "device-pool-fake")
    assert dev.kind == "sandbox"
    assert dev.fake is True
    assert set(dev.injectors) == {"lose_sandbox", "fail_create", "expire_lifetime"}
    assert dev.spec_factory is not None

    perf = next(t for t in targets if t.name == "perf-host-fake")
    assert perf.kind == "sandbox"
    assert perf.fake is True
    assert set(perf.injectors) == {"lose_sandbox", "fail_create", "expire_lifetime"}
    assert perf.spec_factory is not None
