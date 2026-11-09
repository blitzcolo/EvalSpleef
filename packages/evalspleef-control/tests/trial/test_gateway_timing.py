"""The agent.step event carries the gateway's per-request timing of its turn."""

from __future__ import annotations

from typing import Any

from evalspleef.spi.gateway import PriceSpec
from evalspleef.spi.model import UpstreamRoute
from evalspleef.spi.runner import ShellTurn
from evalspleef.testing.scripted_runner import ScriptedRunner


class _TimedRunner(ScriptedRunner):
    """Its first turn makes one timed and one untimed request through the gateway."""

    def __init__(self) -> None:
        super().__init__()
        self.gateway: Any = None
        self.trial_id = ""
        self.sent = 0

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        self.sent += 1
        if self.sent == 1:
            self.gateway.record_usage(
                trial_id=self.trial_id,
                round=1,
                route_name="ep_timed",
                input_tokens=100,
                output_tokens=9,
                cached_tokens=40,
                ttft_ms=812.5,
                e2e_ms=1500.0,
                tpot_ms=15.0,
                itl_mean_ms=14.0,
                itl_p95_ms=30.0,
                itl_max_ms=55.0,
                itl_count=8,
            )
            self.gateway.record_usage(
                trial_id=self.trial_id,
                round=1,
                route_name="ep_timed",
                input_tokens=50,
                output_tokens=5,
            )
        return await super().send(message, deadline_s=deadline_s)


async def test_agent_step_records_the_turns_gateway_request_timing(harness_factory: Any) -> None:
    runner = _TimedRunner()
    h = await harness_factory(scripted=runner)
    await h.gateway.upsert_route(
        "ep_timed",
        UpstreamRoute(
            api_base="http://upstream.invalid/v1",
            model="m",
            protocol="chat_completions",
            api_key_ref="vault://models/test",
        ),
    )
    await h.gateway.set_price("ep_timed", PriceSpec(input_per_mtok=0.0, output_per_mtok=0.0))
    runner.gateway, runner.trial_id = h.gateway, h.trial.trial_id

    await h.run()

    steps = await h.of_type("agent.step")
    first, *rest = steps
    timed, untimed = first.payload["gateway_requests"]
    assert timed["endpoint_id"] == "ep_timed"
    assert (timed["ttft_ms"], timed["e2e_ms"], timed["itl_count"]) == (812.5, 1500.0, 8)
    assert (timed["cached_tokens"], timed["output_tokens"]) == (40, 9)
    assert untimed["ttft_ms"] is None and untimed["output_tokens"] == 5
    # Each request is reported once, on the step of the turn that made it.
    assert all(step.payload["gateway_requests"] == [] for step in rest)
