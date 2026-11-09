"""The benchmark runner, its gateway sampler and the stored record."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from evalspleef.control.bench import (
    BenchPlan,
    BenchRunner,
    Shape,
    bench_prompt,
    hourly_price,
    parse_shape,
    raw_record,
    raw_sha256,
    save,
)
from evalspleef.control.probes.client import GatewayClient
from evalspleef.control.store import BenchmarkRepository, connect, migrate
from evalspleef.core.perf import RequestSample, bench_result
from evalspleef.spi.model import DeploymentSpec, ModelSpec, VendorSource
from evalspleef.testing.clock import FakeClock

SHAPE = Shape(100, 20)


def _plan(**overrides: Any) -> BenchPlan:
    values: dict[str, Any] = {
        "shapes": (SHAPE,),
        "seed": 7,
        "single_repeats": 2,
        "concurrency": (1, 2, 4, 8),
        "requests_per_worker": 5,
    }
    values.update(overrides)
    return BenchPlan(**values)


def _index(body: Mapping[str, Any]) -> int:
    first_line = str(body["messages"][0]["content"]).splitlines()[0]
    return int(first_line.split()[1].split("-")[0])


class ScriptedEngine:
    """Answers each prompt index with a fixed sample: an engine that saturates at 2.

    Every sweep worker's requests occupy [base + 2s, base + 2s + 2] for step s, so a level's
    window is [base + 1, base + 9] and holds 4 requests per worker: system decode is
    concurrency * 4 * output / 8. Output tokens are 20, 20, 10, 5 at concurrency 1, 2, 4, 8,
    so decode is 10, 20, 20, 20: the step to 4 gains 0% and the sweep must stop there.
    """

    OUTPUT: ClassVar[dict[int, int]] = {1: 20, 2: 20, 4: 10, 8: 5}

    def __init__(self, plan: BenchPlan) -> None:
        self.calls: list[int] = []
        self._level_of: dict[int, tuple[int, int]] = {}
        index = len(plan.shapes) * plan.single_repeats
        for concurrency in plan.concurrency:
            for slot in range(concurrency * plan.requests_per_worker):
                self._level_of[index + slot] = (concurrency, slot % plan.requests_per_worker)
            index += concurrency * plan.requests_per_worker

    async def __call__(self, body: Mapping[str, Any]) -> RequestSample:
        index = _index(body)
        self.calls.append(index)
        if index not in self._level_of:  # the single-request phase
            return RequestSample(0.0, 1.5, (0.5, 1.5), input_tokens=100, output_tokens=20)
        concurrency, step = self._level_of[index]
        base = 100.0 * concurrency
        start = base + 2.0 * step
        return RequestSample(
            start,
            start + 2.0,
            (start + 1.0, start + 2.0),
            input_tokens=100,
            output_tokens=self.OUTPUT[concurrency],
        )


async def test_the_sweep_stops_where_a_step_gains_under_five_percent() -> None:
    plan = _plan()
    engine = ScriptedEngine(plan)
    seen: list[tuple[int, float | None]] = []
    samples = await BenchRunner(
        plan,
        engine,
        model_name="ep_demo",
        on_level=lambda shape, level: seen.append((level.concurrency, level.system_decode_tok_s)),
    ).run()

    assert seen == [(1, 10.0), (2, 20.0), (4, 20.0)]
    # 2 single requests, then 1 + 2 + 4 workers x 5 requests; concurrency 8 never ran.
    assert len(engine.calls) == 2 + 7 * 5
    assert len(set(engine.calls)) == len(engine.calls)

    raw = raw_record(
        bench_id="bn_test",
        endpoint_id="ep_demo",
        model_name="ep_demo",
        created_at="2026-11-09T00:00:00Z",
        plan=plan,
        samples=samples,
        price_per_hour_usd=0.72,
        clock="fake",
    )
    result = bench_result(json.loads(json.dumps(raw)))
    (sweep,) = result["sweep"]
    assert sweep["saturation"]["concurrency"] == 2
    assert sweep["saturation"]["reason"] == "gain_below_threshold"
    assert sweep["full_load"]["system_decode_tok_s"] == 20.0
    # 0.72 USD/h / (20 tok/s x 0.0036) = 10 USD per million output tokens
    assert sweep["usd_per_million_output_tokens"] == 10.0
    (single,) = result["single"]
    assert single["metrics"]["ttft_s"]["p50"] == 0.5


def test_prompts_are_deterministic_and_never_share_a_prefix() -> None:
    runner = BenchRunner(_plan(), ScriptedEngine(_plan()), model_name="ep_demo")
    assert runner.body(SHAPE, 3) == runner.body(SHAPE, 3)
    first_lines = {bench_prompt(7, index, 2048).splitlines()[0] for index in range(50)}
    assert len(first_lines) == 50
    assert bench_prompt(7, 1, 2048) != bench_prompt(8, 1, 2048)
    body = runner.body(SHAPE, 0)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 20 and "ignore_eos" not in body


def test_shapes_and_plans_are_validated() -> None:
    assert parse_shape("2048x128") == Shape(2048, 128)
    for text in ("2048", "x128", "2048x1", "ax2"):
        with pytest.raises(ValueError):
            parse_shape(text)
    with pytest.raises(ValueError, match="rise"):
        _plan(concurrency=(4, 2))
    assert _plan().sweep == (SHAPE,)


class TimedSSE(httpx.AsyncByteStream):
    def __init__(self, clock: FakeClock, steps: Sequence[tuple[float, str]]) -> None:
        self._clock = clock
        self._steps = steps

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, text in self._steps:
            self._clock.advance(delay)
            yield text.encode()


def _chunk(delta: dict[str, Any]) -> str:
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + "\n\n"


async def test_the_gateway_sampler_stamps_every_generated_chunk_on_its_clock() -> None:
    clock = FakeClock(start=50.0)
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 20},
    }
    steps = (
        (0.1, _chunk({"role": "assistant"})),
        (0.3, _chunk({"content": "a"})),
        (0.05, _chunk({"content": "b"})),
        (0.15, _chunk({"reasoning_content": "c"})),
        (0.0, "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"),
        (0.2, "data: [DONE]\n\n"),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=TimedSSE(clock, steps))
    )
    async with GatewayClient(
        "http://gw.test",
        "secret",
        clock=clock,
        request_timeout_s=30,
        connect_timeout_s=5,
        transport=transport,
    ) as client:
        sample = await client.stream_sample({"model": "ep", "stream": True})
    assert sample.ok and sample.error is None
    assert sample.t_start == 50.0
    assert sample.chunk_times == pytest.approx((50.4, 50.45, 50.6))
    assert sample.t_end == pytest.approx(50.8)
    assert (sample.input_tokens, sample.cached_tokens, sample.output_tokens) == (120, 20, 3)


async def test_failed_requests_become_failed_samples() -> None:
    clock = FakeClock()

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow"}})

    def no_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(_chunk({"content": "x"}) + "data: [DONE]\n\n").encode())

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream slow", request=request)

    errors = []
    for handler in (refuse, no_usage, broken):
        async with GatewayClient(
            "http://gw.test",
            "secret",
            clock=clock,
            request_timeout_s=30,
            connect_timeout_s=5,
            transport=httpx.MockTransport(handler),
        ) as client:
            sample = await client.stream_sample({"model": "ep", "stream": True})
        assert not sample.ok
        errors.append(sample.error or "")
    assert errors[0].startswith("HTTP 429 rate_limited")
    assert errors[1] == "no usage chunk in the stream"
    assert errors[2].startswith("ReadTimeout")


async def test_save_writes_raw_and_result_side_by_side_and_the_result_recomputes(
    tmp_path: Path,
) -> None:
    plan = _plan(concurrency=(1, 2))
    samples = await BenchRunner(plan, ScriptedEngine(plan), model_name="ep_demo").run()
    raw = raw_record(
        bench_id="bn_saved",
        endpoint_id="ep_demo",
        model_name="ep_demo",
        created_at="2026-11-09T00:00:00Z",
        plan=plan,
        samples=samples,
        price_per_hour_usd=None,
        clock="fake",
    )
    db = await connect("sqlite:///:memory:")
    await migrate(db)
    repository = BenchmarkRepository(db)
    saved = await save(
        raw, facts={"serving": "api"}, directory=tmp_path / "bench", repository=repository
    )
    assert saved.raw_path.parent == saved.result_path.parent == tmp_path / "bench" / "bn_saved"
    stored_raw = json.loads(saved.raw_path.read_text(encoding="utf-8"))
    document = json.loads(saved.result_path.read_text(encoding="utf-8"))
    assert raw_sha256(stored_raw) == document["raw_sha256"] == saved.row.raw_sha256
    recomputed = bench_result(stored_raw)
    assert {key: document[key] for key in recomputed} == recomputed
    row = await repository.latest("ep_demo")
    assert row is not None and row.result == document and row.facts == {"serving": "api"}
    with pytest.raises(ValueError, match="already exists"):
        await repository.insert(row)


def _spec(params: dict[str, Any]) -> ModelSpec:
    return ModelSpec(
        slug="demo",
        spec_version=1,
        family="demo",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="demo", model_id="demo"),
        deployment=DeploymentSpec(adapter="openai-compatible", account="a", params=params),
        declared_context_tokens=1000,
        declared_max_output_tokens=100,
        pricing_ref="assumed:x",
        license="MIT",
        owner="t",
    )


def test_hourly_price_comes_from_the_deployment_params() -> None:
    assert hourly_price(_spec({"price_per_hour_usd": 4.65})) == 4.65
    assert hourly_price(_spec({"price_per_gpu_hour_usd": 2.325, "gpu_count": 2})) == 4.65
    assert hourly_price(_spec({"price_per_gpu_hour_usd": 1.5})) == 1.5
    assert hourly_price(_spec({"pricing": {"input_per_mtok": 1}})) is None
