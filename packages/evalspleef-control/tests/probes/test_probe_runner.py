"""Probe suite P-01..P-15 over real HTTP: runner -> gateway stand-in -> FakeOpenAIServer."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from evalspleef.control.probes import (
    PROBE_IDS,
    ProbeConfig,
    ProbeRunner,
    ProbeTarget,
    build_report,
    capabilities_from_dict,
    capabilities_to_dict,
)
from evalspleef.spi.gateway import GatewayAdmin
from evalspleef.spi.probe import ProbeCheck

# The ``Stack`` dataclass lives in conftest.py (not importable under importlib mode).
Stack = Any
MakeStack = Callable[..., Stack]

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
}
BROKEN_TOOL_CALL = {
    "id": "call_2",
    "type": "function",
    "function": {"name": "get_weather", "arguments": "{not json"},
}


def target(stack: Stack, **overrides: Any) -> ProbeTarget:
    fields: dict[str, Any] = {
        "endpoint_id": "ep_test_0001",
        "model_name": stack.route_name,
        "declared_context_tokens": 4096,
        "declared_max_output_tokens": 1000,
        "expected_models": frozenset({stack.server.model, stack.route_name}),
    }
    fields.update(overrides)
    return ProbeTarget(**fields)


def runner(stack: Stack, admin: GatewayAdmin | None = None, **config: Any) -> ProbeRunner:
    return ProbeRunner(
        admin or stack.admin,
        gateway_url=stack.standin.url,
        config=ProbeConfig(request_timeout_s=30.0, long_request_timeout_s=60.0, **config),
    )


async def test_full_suite_measures_every_capability_through_the_gateway(
    make_stack: MakeStack,
) -> None:
    stack = make_stack(
        parallel_tool_calls=True,
        reasoning_content=True,
        system_fingerprint="fp-1",
        context_limit_tokens=2000,
        long_output_words=3000,
    )
    report = await runner(stack, context_resolution_tokens=64).run(
        target(stack, expected_system_fingerprint="fp-1")
    )

    # P-15 is optional and off by default; everything else ran, in order.
    assert [check.probe_id for check in report.checks] == list(PROBE_IDS[:14])
    failed = {check.probe_id: check.detail for check in report.checks if not check.passed}
    assert failed == {}

    caps = report.capabilities
    assert caps.chat and caps.streaming and caps.usage_in_stream
    assert caps.tool_calling == "parallel"
    assert caps.tool_call_json_valid_rate == 1.0
    assert caps.multi_turn_tools
    assert caps.tool_choice_modes == frozenset({"required", "none"})
    assert caps.json_schema
    assert caps.reasoning_field == "reasoning_content"
    assert caps.reasoning_toggle
    # Bisection brackets the fake's 2000-token limit (the prompt carries ~15 extra words).
    assert 1900 <= caps.context_tokens_effective <= 1985
    assert caps.max_output_tokens_effective == 1000
    assert caps.seed_deterministic and caps.stop_ok
    assert caps.protocols_ok == frozenset({"chat_completions", "anthropic_messages", "responses"})
    assert caps.identity_ok
    assert set(caps.throughput) == {"1", "4", "16"}
    for stats in caps.throughput.values():
        assert set(stats) == {"ttft_p50", "ttft_p95", "tps_p50", "tps_p95"}
        assert stats["ttft_p95"] >= stats["ttft_p50"] >= 0.0

    # P-12 records the benchmark figures per level from the same samples.
    p12 = next(check for check in report.checks if check.probe_id == "P-12")
    for level in ("1", "4", "16"):
        figures = p12.data["perf"][level]
        assert figures["requests"]["ok"] == figures["requests"]["with_ttft"] == 20
        assert figures["level"]["concurrency"] == int(level)
        assert figures["requests"]["e2e_s"]["n"] == 20
    # P-12 sent 3 x 20 streaming requests (plus the P-02 stream), all via the gateway.
    streamed = [r for r in stack.server.requests if r.body.get("stream")]
    assert len(streamed) == 1 + 3 * 20
    paths = {path for path, _model in stack.standin.requests}
    assert paths == {"/v1/chat/completions", "/v1/messages", "/v1/responses"}
    assert {model for _path, model in stack.standin.requests} == {stack.route_name}
    # The probe key is revoked once the run ends.
    assert stack.admin.is_revoked("tk_1")


async def test_failed_chat_skips_the_remaining_probes(make_stack: MakeStack) -> None:
    stack = make_stack()
    stack.server.inject_error(500)
    report = await runner(stack).run(target(stack))

    first, *rest = report.checks
    assert first.probe_id == "P-01" and not first.passed
    assert len(report.checks) == 14
    assert all(check.data.get("skipped") for check in rest)
    assert not report.capabilities.chat
    assert len(stack.server.requests) == 1


async def test_context_probe_leaves_a_reasoning_model_room_to_answer(make_stack: MakeStack) -> None:
    """Real reasoning models (DeepSeek, GLM) answered nothing within a 32-token budget,
    which measured their context as 0 and skipped every task."""
    stack = make_stack(context_limit_tokens=3000, thinking_tokens=200, reasoning_content=True)
    report = await runner(stack, include=frozenset({"P-01", "P-09"})).run(target(stack))

    by_id = {check.probe_id: check for check in report.checks}
    assert by_id["P-09"].passed, by_id["P-09"].detail
    assert 2000 < by_id["P-09"].data["effective"] <= 3000


async def test_identity_probe_detects_a_different_responding_model(make_stack: MakeStack) -> None:
    stack = make_stack(reported_model="some-other-model")
    report = await runner(stack, include=frozenset({"P-01", "P-14"})).run(target(stack))

    by_id = {check.probe_id: check for check in report.checks}
    assert by_id["P-01"].passed
    assert not by_id["P-14"].passed
    assert by_id["P-14"].data["response_model"] == "some-other-model"
    assert not report.capabilities.identity_ok


async def test_fingerprint_probe_is_optional_and_stable(make_stack: MakeStack) -> None:
    stack = make_stack()
    config = {"enable_fingerprint": True, "include": frozenset({"P-15"})}
    first = await runner(stack, **config).run(target(stack))
    second = await runner(stack, **config).run(target(stack))

    assert first.capabilities.fingerprint is not None
    assert first.capabilities.fingerprint == second.capabilities.fingerprint

    mismatch = await runner(stack, fingerprint_reference="0" * 64, **config).run(target(stack))
    (check,) = mismatch.checks
    assert not check.passed
    assert check.data["matches_reference"] is False


@pytest.mark.parametrize(
    ("replies", "level", "rate"),
    [
        ([TOOL_CALL] * 5, "single", 1.0),
        ([TOOL_CALL] * 3 + [BROKEN_TOOL_CALL] * 2, "none", 0.6),
    ],
)
async def test_tool_call_validity_rate_decides_the_level(
    make_stack: MakeStack, replies: list[dict[str, Any]], level: str, rate: float
) -> None:
    stack = make_stack(parallel_tool_calls=False)
    for call in replies:
        stack.server.queue_reply(None, tool_calls=(call,), finish_reason="tool_calls")
    report = await runner(stack, include=frozenset({"P-03"})).run(target(stack))

    assert report.capabilities.tool_calling == level
    assert report.capabilities.tool_call_json_valid_rate == pytest.approx(rate)


async def test_cold_start_reported_by_the_gateway_is_recorded(make_stack: MakeStack) -> None:
    stack = make_stack(cold_start_window_s=30)
    stack.server.inject_error(503, count=2)
    report = await runner(stack, include=frozenset({"P-01"})).run(target(stack))

    (check,) = report.checks
    assert check.passed
    assert check.data["cold_start_ms"] >= 1
    assert len(stack.server.requests) == 3  # two 503s absorbed by the gateway, then success


async def test_ping_is_a_single_token_request_and_revokes_its_key(make_stack: MakeStack) -> None:
    stack = make_stack()
    probe_runner = runner(stack)

    ok = await probe_runner.ping("ep_test_0001", stack.route_name)
    assert ok.ok and ok.status == 200
    assert stack.server.requests[-1].body["max_tokens"] == 1
    assert stack.admin.is_revoked("tk_1")

    missing = await probe_runner.ping("ep_test_0001", "ep_unknown")
    assert not missing.ok and missing.status == 404
    assert stack.admin.is_revoked("tk_2")


def test_report_id_is_stable_and_sensitive_to_content() -> None:
    checks = {"P-01": ProbeCheck("P-01", True, "ok", {"latency_s": 0.5})}
    first = build_report("ep_a", "gw-1", "2026-11-07T00:00:00Z", checks)
    again = build_report("ep_a", "gw-1", "2026-11-07T00:00:00Z", checks)
    other = build_report(
        "ep_a", "gw-1", "2026-11-07T00:00:00Z", {"P-01": ProbeCheck("P-01", False, "ok")}
    )
    later = build_report("ep_a", "gw-1", "2026-11-07T00:00:01Z", checks)

    assert first.report_id == again.report_id
    assert len({first.report_id, other.report_id, later.report_id}) == 3


def test_capabilities_survive_a_json_round_trip() -> None:
    checks = {
        "P-01": ProbeCheck("P-01", True),
        "P-03": ProbeCheck("P-03", True, data={"valid_rate": 0.8}),
        "P-06": ProbeCheck("P-06", True, data={"modes": ["none", "required"]}),
        "P-09": ProbeCheck("P-09", True, data={"effective": 12345}),
        "P-12": ProbeCheck("P-12", True, data={"throughput": {"4": {"tps_p50": 9.5}}}),
        "P-13": ProbeCheck("P-13", False, data={"protocols": ["responses"]}),
    }
    caps = build_report("ep_a", "gw-1", "2026-11-07T00:00:00Z", checks).capabilities
    assert caps.protocols_ok == frozenset({"chat_completions", "responses"})

    stored = json.loads(json.dumps(capabilities_to_dict(caps)))
    assert capabilities_from_dict(stored) == caps
