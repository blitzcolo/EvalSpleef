"""Admission driven by a probe report that went through the real HTTP probe and the stores."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from evalspleef.control.admission import admit_cells
from evalspleef.spi.caps import ModelRequirements
from evalspleef.spi.model import ModelSpec

MakeWorld = Callable[..., Awaitable[Any]]
SpecFactory = Callable[..., ModelSpec]


async def test_cells_are_admitted_from_the_stored_probe_report(
    make_world: MakeWorld, spec_factory: SpecFactory
) -> None:
    # Without parallel_tool_calls the fake upstream never answers with a tool call on "auto".
    world = await make_world(mode="external")
    handle = await world.manager.register(spec_factory())
    await world.manager.probe(handle.endpoint_id)
    report = await world.manager.latest_report(handle.endpoint_id)

    result = admit_cells(
        {handle.endpoint_id: report},
        {
            "plain": ModelRequirements(),
            "agentic": ModelRequirements(tool_calling="required"),
            "long-context": ModelRequirements(min_context_tokens=5000),
            "fits": ModelRequirements(min_context_tokens=4096),
        },
    )

    outcomes = {d.task: (d.outcome.value, d.reasons) for d in result.decisions}
    assert outcomes == {
        "plain": ("run", ()),
        "agentic": ("skip", ("capability:tool_calling",)),
        "long-context": ("skip", ("capability:context_tokens_effective",)),
        "fits": ("run", ()),
    }
