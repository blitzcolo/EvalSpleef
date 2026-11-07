from __future__ import annotations

from evalspleef_model_vpc_vllm.conformance import build_targets


def test_build_conformance_targets() -> None:
    targets = build_targets()
    assert len(targets) >= 1
    target = targets[0]

    assert target.name == "vpc-vllm-fake"
    assert target.kind == "model"
    assert target.fake is True
    assert target.model_fixture is not None
    assert "upstream_status" in target.injectors
