"""The lifecycle table against TECH-DESIGN section 6.2, and its enforcement by the manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from evalspleef.control.endpoints import TRANSITIONS, InvalidTransition, event_type_for
from evalspleef.spi.model import EndpointState, ModelSpec

S = EndpointState
MakeWorld = Callable[..., Awaitable[Any]]
SpecFactory = Callable[..., ModelSpec]

# Every arrow of the state diagram (plus FAILED/DEGRADED fallbacks), as (from, to).
DIAGRAM_EDGES = [
    (S.REGISTERED, S.REJECTED),  # validate fails
    (S.REGISTERED, S.AWAITING_OPERATOR),  # assisted deploy
    (S.REGISTERED, S.DEPLOYING),  # api deploy in flight
    (S.REGISTERED, S.VERIFYING),  # external endpoint
    (S.AWAITING_OPERATOR, S.VERIFYING),  # attach
    (S.AWAITING_OPERATOR, S.FAILED),  # 24 h without an operator
    (S.DEPLOYING, S.VERIFYING),
    (S.DEPLOYING, S.FAILED),
    (S.VERIFYING, S.READY),  # probe ok
    (S.VERIFYING, S.PROBE_FAILED),
    (S.READY, S.WARM),
    (S.WARM, S.READY),
    (S.READY, S.SLEEPING),
    (S.WARM, S.SLEEPING),
    (S.SLEEPING, S.WARM),  # wake()
    (S.SLEEPING, S.RETIRED),  # teardown(terminate)
    (S.READY, S.RETIRED),
]
FORBIDDEN_EDGES = [
    (S.REJECTED, S.READY),
    (S.AWAITING_OPERATOR, S.READY),  # never READY without a probe
    (S.PROBE_FAILED, S.READY),
    (S.RETIRED, S.WARM),
    (S.RETIRED, S.REGISTERED),
    (S.FAILED, S.WARM),
    (S.READY, S.AWAITING_OPERATOR),
]


def test_every_state_has_a_row_and_the_diagram_edges_are_allowed() -> None:
    assert set(TRANSITIONS) == set(EndpointState)
    for source, target in DIAGRAM_EDGES:
        assert target in TRANSITIONS[source], f"{source.value} -> {target.value}"


def test_edges_outside_the_diagram_are_forbidden() -> None:
    for source, target in FORBIDDEN_EDGES:
        assert target not in TRANSITIONS[source], f"{source.value} -> {target.value}"
    assert TRANSITIONS[S.RETIRED] == frozenset()


def test_every_state_maps_to_one_of_the_core_endpoint_event_types() -> None:
    types = {str(event_type_for(state)) for state in EndpointState}
    assert types <= {
        "endpoint.registered",
        "endpoint.probe.result",
        "endpoint.warm",
        "endpoint.unavailable",
        "endpoint.retired",
    }
    assert str(event_type_for(S.READY)) == "endpoint.probe.result"
    assert str(event_type_for(S.WARM)) == "endpoint.warm"


async def test_the_manager_refuses_an_illegal_transition_without_writing_an_event(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    world = await make_world()
    handle = await world.manager.register(spec_factory())
    assert handle.state is S.AWAITING_OPERATOR
    events_before = len(await world.events_in())

    with pytest.raises(InvalidTransition):
        await world.manager._transition(handle, S.READY, reason="skip the probe")

    assert len(await world.events_in()) == events_before
    assert (await world.manager.get(handle.endpoint_id)).state is S.AWAITING_OPERATOR
