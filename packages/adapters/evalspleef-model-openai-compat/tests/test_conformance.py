"""Conformance target contract tests: target shape and a real-HTTP round trip."""

from __future__ import annotations

from evalspleef_model_openai_compat.conformance import build_targets

from evalspleef.spi.model import EndpointState


async def test_target_shape() -> None:
    targets = build_targets()

    assert len(targets) == 1
    target = targets[0]
    assert target.name == "openai-compat-fake"
    assert target.kind == "model"
    assert target.fake is True
    assert target.model_fixture is not None
    assert target.model_fixture.upstream is not None
    assert target.model_fixture.operator_input_factory is None
    assert set(target.injectors) == {"upstream_status", "kill_host"}
    assert target.cleanup is not None
    await target.cleanup()


async def test_provider_round_trip_through_target() -> None:
    target = build_targets()[0]
    fixture = target.model_fixture
    assert fixture is not None and fixture.upstream is not None
    try:
        spec = fixture.spec_factory()
        report = await target.provider.validate(spec)
        assert report.ok is True, report.checks

        handle = await target.provider.deploy(spec, await target.provider.plan(spec))
        assert handle.state is EndpointState.VERIFYING
        assert await target.provider.status(handle) is EndpointState.READY

        route = target.provider.upstream(handle)
        assert route.api_base == fixture.upstream.base_url

        await target.injectors["upstream_status"](503)
        assert await target.provider.status(handle) is EndpointState.FAILED

        await target.injectors["kill_host"]()
        assert await target.provider.status(handle) is EndpointState.FAILED
    finally:
        await target.cleanup()
