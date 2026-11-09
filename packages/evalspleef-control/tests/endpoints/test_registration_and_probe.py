"""Registration, assisted attach, probing and ticket expiry over the real HTTP path."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from evalspleef.control.endpoints import InvalidTransition, make_endpoint_id, public_name
from evalspleef.control.probes import (
    ProbeConfig,
    ProbeRunner,
    ProbeTarget,
    iso_utc,
    report_from_row,
)
from evalspleef.spi.errors import EndpointMismatch
from evalspleef.spi.model import EndpointState, ModelSpec

MakeWorld = Callable[..., Awaitable[Any]]
SpecFactory = Callable[..., ModelSpec]

API_BASE = "https://models.example.test/v1"
MODEL_ID = "qwen-32b"


async def test_assisted_flow_rejects_bad_input_then_reaches_ready(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    handle = await world.manager.register(spec_factory())
    endpoint_id = handle.endpoint_id
    name = public_name(endpoint_id)
    assert handle.state is EndpointState.AWAITING_OPERATOR
    (ticket,) = await world.manager.tickets(endpoint_id)
    assert (ticket.kind, ticket.status) == ("deploy", "open")
    assert ticket.instructions_md

    wrong_url = {"base_url": "http://models.example.test/v1", "model": MODEL_ID}
    with pytest.raises(EndpointMismatch) as bad_url:
        await world.manager.attach(endpoint_id, wrong_url)
    assert bad_url.value.detail["reason"] == "base_url_not_https"
    with pytest.raises(EndpointMismatch) as bad_model:
        await world.manager.attach(endpoint_id, {"base_url": API_BASE, "model": "other-model"})
    assert bad_model.value.detail["reason"] == "model_mismatch"

    # Two rejected attempts changed nothing: same state, open ticket, no route, no traffic.
    assert (await world.manager.get(endpoint_id)).state is EndpointState.AWAITING_OPERATOR
    assert [t.status for t in await world.manager.tickets(endpoint_id)] == ["open"]
    assert world.admin.routes == {}
    assert world.server.requests == []

    ready = await world.manager.attach(
        endpoint_id,
        {"base_url": API_BASE, "model": MODEL_ID, "deployment_id": "dep-1", "operator": "alice"},
    )

    assert ready.state is EndpointState.READY
    assert ready.external_ids["deployment_id"] == "dep-1"
    # The route is registered under the public name and points at what the operator pasted.
    route = world.admin.routes[name]
    assert (route.api_base, route.model) == (API_BASE, MODEL_ID)
    # The price was set too: the in-memory gateway can now cost a request on that route.
    usage = world.admin.record_usage(
        trial_id="t1", round=1, route_name=name, input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert usage.cost_usd == pytest.approx(3.0)
    # The probe ran through the gateway with a key that has been revoked again.
    assert {path for path, _model in world.standin.requests} >= {"/v1/chat/completions"}
    assert world.admin.is_revoked("tk_1")

    report = await world.manager.latest_report(endpoint_id)
    assert report is not None and report.report_id == ready.observed["report_id"]
    row = await world.reports.get(report.report_id)
    assert row is not None and report_from_row(row) == report
    passed = {check.probe_id for check in report.checks if check.passed}
    assert {"P-01", "P-02", "P-14"} <= passed
    assert report.capabilities.identity_ok
    assert report.capabilities.context_tokens_effective == 4096

    (closed,) = await world.manager.tickets(endpoint_id)
    assert (closed.status, closed.operator) == ("closed", "alice")
    assert closed.closed_at is not None

    assert await world.transitions() == [
        ("endpoint.registered", None, "registered"),
        ("endpoint.registered", "registered", "awaiting_operator"),
        ("endpoint.registered", "awaiting_operator", "verifying"),
        ("endpoint.probe.result", "verifying", "ready"),
    ]
    await world.assert_row_matches_log(endpoint_id)


async def test_failed_probe_removes_the_route_and_a_reprobe_recovers(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world(mode="external", reported_model="drifted-model")
    handle = await world.manager.register(spec_factory())
    endpoint_id, name = handle.endpoint_id, public_name(handle.endpoint_id)
    assert handle.state is EndpointState.VERIFYING

    failed = await world.manager.probe(endpoint_id)

    assert (await world.manager.get(endpoint_id)).state is EndpointState.PROBE_FAILED
    assert name not in world.admin.routes
    events = await world.events_in()
    assert events[-1].payload["failed_probes"] == ["P-14"]
    assert events[-1].payload["report_id"] == failed.report_id
    assert await world.reports.get(failed.report_id) is not None

    world.server.reported_model = MODEL_ID  # the upstream is fixed
    recovered = await world.manager.probe(endpoint_id)

    assert (await world.manager.get(endpoint_id)).state is EndpointState.READY
    assert name in world.admin.routes
    assert recovered.report_id != failed.report_id
    assert [t[1:] for t in (await world.transitions())[-3:]] == [
        ("verifying", "probe_failed"),
        ("probe_failed", "verifying"),
        ("verifying", "ready"),
    ]
    await world.assert_row_matches_log(endpoint_id)


async def test_judge_role_needs_structured_output_but_target_does_not(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    config = ProbeConfig(
        request_timeout_s=30.0, include=frozenset({"P-01", "P-02", "P-07", "P-14"})
    )
    world = await make_world(mode="external", probe_config=config)
    # P-01, P-02 and P-07 consume the first three replies: the P-07 answer is not JSON.
    for reply in ("ok", "one two", "not json"):
        world.server.queue_reply(reply)

    judge = await world.manager.register(spec_factory(slug="judge", roles=frozenset({"judge"})))
    await world.manager.probe(judge.endpoint_id)
    assert (await world.manager.get(judge.endpoint_id)).state is EndpointState.PROBE_FAILED
    assert (await world.events_in())[-1].payload["failed_probes"] == ["P-07"]

    target = await world.manager.register(spec_factory(slug="target"))
    await world.manager.probe(target.endpoint_id)
    assert (await world.manager.get(target.endpoint_id)).state is EndpointState.READY


async def test_huggingface_source_is_resolved_checked_and_mirrored_before_deploy(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    spec = spec_factory(hf=True)
    world.hf.preflight_failures = (("gated_license", False, "license not accepted"),)

    rejected = await world.manager.register(spec)

    assert rejected.state is EndpointState.REJECTED
    assert await world.models.get("qwen", 1) is None  # an unusable spec is never frozen
    assert await world.manager.tickets(rejected.endpoint_id) == []  # and nothing was deployed
    assert world.hf.calls == ["resolve", "preflight"]
    last = (await world.events_in())[-1]
    assert (str(last.type), last.payload["state"]) == ("endpoint.unavailable", "rejected")
    assert last.payload["failures"] == ["gated_license: license not accepted"]

    world.hf.preflight_failures = ()  # the operator accepted the licence
    retried = await world.manager.register(spec)

    assert retried.state is EndpointState.AWAITING_OPERATOR
    assert world.hf.calls[2:] == ["resolve", "preflight", "hashes", "mirror"]
    frozen = await world.models.get("qwen", 1)
    assert frozen is not None
    source = frozen.source
    assert (source.revision, dict(source.files_sha256), source.mirror_repo) == (  # type: ignore[union-attr]
        "a" * 40,
        {"config.json": "c0ffee"},
        "our-org/mirror-qwen-aaaaaaa",
    )
    assert [t[1:] for t in (await world.transitions())[-3:]] == [
        ("registered", "rejected"),
        ("rejected", "registered"),
        ("registered", "awaiting_operator"),
    ]


async def test_deploy_ticket_expires_after_24_hours_and_fails_the_endpoint(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    handle = await world.manager.register(spec_factory())
    (ticket,) = await world.manager.tickets(handle.endpoint_id)

    # The CLI text carries the machine-readable facts: ids and the exact expiry instant.
    text = world.manager.render_ticket(ticket)
    assert ticket.ticket_id in text and handle.endpoint_id in text
    assert iso_utc(world.clock.now() + 86_400) in text

    world.clock.advance(86_400 - 1)
    assert await world.manager.expire_tickets() == []
    assert (await world.manager.get(handle.endpoint_id)).state is EndpointState.AWAITING_OPERATOR

    world.clock.advance(1)
    assert await world.manager.expire_tickets() == [handle.endpoint_id]

    assert (await world.manager.get(handle.endpoint_id)).state is EndpointState.FAILED
    (expired,) = await world.manager.tickets(handle.endpoint_id)
    assert expired.status == "expired"
    last = (await world.events_in())[-1]
    assert (str(last.type), last.payload["reason"]) == ("endpoint.unavailable", "ticket_expired")
    await world.assert_row_matches_log(handle.endpoint_id)


async def test_registering_twice_is_idempotent_and_operations_check_the_state(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    spec = spec_factory()
    first = await world.manager.register(spec)
    events_before = len(await world.events_in())

    again = await world.manager.register(spec)

    assert again.endpoint_id == first.endpoint_id == make_endpoint_id(spec)
    assert len(await world.events_in()) == events_before
    with pytest.raises(InvalidTransition):
        await world.manager.probe(first.endpoint_id)  # nothing attached yet
    with pytest.raises(InvalidTransition):
        await world.manager.warm([first.endpoint_id], 1)  # not READY


async def test_probe_traffic_is_costed_at_the_route_price_from_the_first_request(
    make_world: MakeWorld, spec_factory: SpecFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is priced before the probe runs, whatever the outcome, so the gateway costs
    probe requests and the probe key's budget binds (they were booked at 0 USD before)."""
    world = await make_world(mode="external", reported_model="drifted-model")
    probe_costs: list[float] = []
    run = ProbeRunner.run

    async def priced_run(self: ProbeRunner, target: ProbeTarget) -> Any:
        # What the gateway does with a request on this route while the probe runs.
        usage = world.admin.record_usage(
            trial_id=f"probe-{target.endpoint_id}",
            round=0,
            route_name=target.model_name,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        probe_costs.append(usage.cost_usd)
        return await run(self, target)

    monkeypatch.setattr(ProbeRunner, "run", priced_run)
    handle = await world.manager.register(spec_factory())
    endpoint_id, name = handle.endpoint_id, public_name(handle.endpoint_id)

    await world.manager.probe(endpoint_id)  # fails P-14: the upstream reports another model
    assert (await world.manager.get(endpoint_id)).state is EndpointState.PROBE_FAILED
    assert name not in world.admin.routes

    world.server.reported_model = MODEL_ID
    await world.manager.probe(endpoint_id)
    assert (await world.manager.get(endpoint_id)).state is EndpointState.READY

    # 1M input at 1.0 + 1M output at 2.0 USD per million, during both probes.
    assert probe_costs == [pytest.approx(3.0), pytest.approx(3.0)]
