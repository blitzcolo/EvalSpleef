"""Inference performance metrics: hand-computed fixtures for every definition."""

from __future__ import annotations

import pytest

from evalspleef.core.perf import (
    LevelMetrics,
    ObservedRequest,
    RequestSample,
    bench_result,
    busy_seconds,
    distribution,
    level_metrics,
    observed_metrics,
    percentile,
    request_metrics,
    saturation,
    set_metrics,
    should_stop,
    steady_window,
    usd_per_million_output_tokens,
)

STREAMED = RequestSample(
    t_start=0.0,
    t_end=3.2,
    chunk_times=(1.0, 1.5, 2.0, 3.0),
    input_tokens=1000,
    cached_tokens=200,
    output_tokens=4,
)


def test_request_metrics_follow_the_definitions() -> None:
    metrics = request_metrics(STREAMED)
    assert metrics.ttft_s == 1.0
    assert metrics.itl_s == (0.5, 0.5, 1.0)
    # TPOT = (t_last - t_first) / (output_tokens - 1) = 2.0 / 3
    assert metrics.tpot_s == pytest.approx(2.0 / 3)
    assert metrics.decode_tok_s == pytest.approx(1.5)
    assert metrics.e2e_s == 3.2


def test_cached_tokens_are_not_prefill_work() -> None:
    # (1000 input - 200 cached) / 1.0 s TTFT
    assert request_metrics(STREAMED).prefill_tok_s == 800.0
    level = level_metrics(
        [
            RequestSample(
                0.0, 2.0, (1.0, 2.0), input_tokens=100, cached_tokens=60, output_tokens=2
            ),
            RequestSample(
                2.0, 4.0, (3.0, 4.0), input_tokens=100, cached_tokens=60, output_tokens=2
            ),
            RequestSample(
                4.0, 6.0, (5.0, 6.0), input_tokens=100, cached_tokens=60, output_tokens=2
            ),
        ],
        concurrency=1,
    )
    # window [0.6, 5.4] holds the first two requests: 2 x 40 uncached tokens over 4.8 s
    assert level.system_prefill_tok_s == pytest.approx(80 / 4.8)


def test_a_request_that_was_not_streamed_has_no_ttft_itl_or_tpot() -> None:
    plain = RequestSample(t_start=5.0, t_end=7.5, input_tokens=10, output_tokens=40, streamed=False)
    metrics = request_metrics(plain)
    assert (metrics.ttft_s, metrics.itl_s, metrics.tpot_s) == (None, (), None)
    assert (metrics.prefill_tok_s, metrics.decode_tok_s) == (None, None)
    assert metrics.e2e_s == 2.5
    summary = set_metrics([STREAMED, plain])
    assert summary.with_ttft == 1
    assert summary.ttft_s.n == 1 and summary.e2e_s.n == 2


def test_percentiles_interpolate_between_ranks() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert percentile(values, 50) == 2.5
    assert percentile(values, 95) == pytest.approx(3.85)
    assert percentile(values, 99) == pytest.approx(3.97)
    assert percentile([], 50) is None
    dist = distribution([1.0, None, 3.0])
    assert (dist.n, dist.mean, dist.p50) == (2, 2.0, 2.0)


def test_failed_requests_are_counted_but_not_measured() -> None:
    failed = RequestSample(0.0, 0.5, ok=False, error="HTTP 500")
    summary = set_metrics([STREAMED, failed])
    assert (summary.requests, summary.ok, summary.failed) == (2, 1, 1)
    assert summary.e2e_s.n == 1


def _sequential(count: int, *, output: int = 10, start: float = 0.0) -> list[RequestSample]:
    return [
        RequestSample(
            t_start=start + 2.0 * index,
            t_end=start + 2.0 * (index + 1),
            chunk_times=(start + 2.0 * index + 1.0, start + 2.0 * (index + 1)),
            input_tokens=100,
            output_tokens=output,
        )
        for index in range(count)
    ]


def test_steady_window_drops_the_first_and_last_tenth() -> None:
    samples = _sequential(5)
    window = steady_window(samples)
    assert window is not None
    assert (window.start, window.end) == (1.0, 9.0)
    level = level_metrics(samples, concurrency=1)
    # requests ending at 2, 4, 6, 8 are inside [1, 9]; the one ending at 10 is not
    assert level.in_window == 4
    assert level.system_decode_tok_s == 40 / 8
    assert level.system_prefill_tok_s == 400 / 8
    assert level.run_s == pytest.approx(10.0)


def _level(concurrency: int, decode: float, failed: int = 0) -> LevelMetrics:
    base = level_metrics(_sequential(3), concurrency=concurrency)
    return LevelMetrics(
        concurrency=concurrency,
        requests=base.requests + failed,
        ok=base.ok,
        failed=failed,
        run_s=base.run_s,
        window=base.window,
        in_window=base.in_window,
        system_prefill_tok_s=base.system_prefill_tok_s,
        system_decode_tok_s=decode,
        metrics=base.metrics,
    )


def test_saturation_is_the_level_before_a_step_gaining_under_five_percent() -> None:
    levels = [_level(1, 10.0), _level(2, 19.0), _level(4, 19.5)]
    decision = saturation(levels)
    assert (decision.concurrency, decision.saturated, decision.reason) == (
        2,
        True,
        "gain_below_threshold",
    )
    assert decision.next_gain == pytest.approx(0.5 / 19.0)
    assert should_stop(levels[1], levels[2])
    assert not should_stop(levels[0], levels[1])


def test_saturation_stops_at_failures_and_reports_an_exhausted_plan() -> None:
    failing = saturation([_level(1, 10.0), _level(2, 30.0, failed=1)])
    assert (failing.concurrency, failing.reason) == (1, "next_level_failed")
    assert should_stop(None, _level(1, 10.0, failed=2))
    rising = saturation([_level(1, 10.0), _level(2, 19.0), _level(4, 35.0)])
    assert (rising.concurrency, rising.saturated, rising.reason) == (4, False, "plan_exhausted")
    first = saturation([_level(1, 0.0, failed=1)])
    assert (first.concurrency, first.reason) == (None, "first_level_failed")


def test_cost_per_million_output_tokens_at_saturation() -> None:
    # 4.65 USD/h / (144.8 tok/s x 0.0036) - the Beam run's figure
    assert usd_per_million_output_tokens(4.65, 144.8) == pytest.approx(8.9204, abs=1e-4)
    assert usd_per_million_output_tokens(None, 144.8) is None
    assert usd_per_million_output_tokens(4.65, None) is None


def _raw_sample(phase: str, concurrency: int, sample: RequestSample) -> dict[str, object]:
    return {
        "phase": phase,
        "concurrency": concurrency,
        "input_target": 100,
        "output_target": 10,
        **sample.to_dict(),
    }


def test_bench_result_is_a_pure_function_of_the_raw_record() -> None:
    sweep_one = _sequential(5)
    sweep_two = [
        *_sequential(5, output=10, start=20.0),
        *_sequential(5, output=10, start=20.0),
    ]
    raw = {
        "price_per_hour_usd": 3.6,
        "samples": [
            _raw_sample("single", 1, STREAMED),
            *(_raw_sample("sweep", 1, s) for s in sweep_one),
            *(_raw_sample("sweep", 2, s) for s in sweep_two),
        ],
    }
    result = bench_result(raw)
    assert result == bench_result(raw)
    (single,) = result["single"]
    assert single["metrics"]["ttft_s"]["p50"] == 1.0
    assert single["metrics"]["prefill_tok_s"]["p50"] == 800.0
    (sweep,) = result["sweep"]
    decodes = [level["system_decode_tok_s"] for level in sweep["levels"]]
    assert decodes == [5.0, 10.0]
    # both levels rise; the plan ran out before throughput stopped rising
    assert sweep["saturation"] == {
        "concurrency": 2,
        "saturated": False,
        "reason": "plan_exhausted",
        "next_gain": None,
    }
    assert sweep["full_load"]["concurrency"] == 2
    # 3.6 USD/h / (10 tok/s x 0.0036) = 100 USD per million output tokens
    assert sweep["usd_per_million_output_tokens"] == 100.0


def test_busy_seconds_is_the_union_of_request_intervals() -> None:
    assert busy_seconds([(6.0, 10.0), (8.0, 12.0), (18.0, 20.0)]) == 8.0
    assert busy_seconds([]) == 0.0


def test_observed_metrics_pool_itl_and_label_throughput_over_busy_time() -> None:
    requests = [
        ObservedRequest(
            t_end=10.0,
            e2e_s=4.0,
            ttft_s=1.0,
            tpot_s=0.1,
            itl_mean_s=0.1,
            itl_p95_s=0.2,
            itl_max_s=0.3,
            itl_count=9,
            input_tokens=100,
            cached_tokens=50,
            output_tokens=10,
        ),
        ObservedRequest(
            t_end=12.0,
            e2e_s=4.0,
            ttft_s=2.0,
            tpot_s=0.2,
            itl_mean_s=0.2,
            itl_p95_s=0.4,
            itl_max_s=0.9,
            itl_count=19,
            input_tokens=100,
            output_tokens=20,
        ),
        ObservedRequest(t_end=20.0, e2e_s=2.0, input_tokens=100, output_tokens=30),
        ObservedRequest(t_end=21.0, e2e_s=None, ok=False),
    ]
    observed = observed_metrics(requests)
    assert (observed["requests"], observed["ok"], observed["with_ttft"]) == (4, 3, 2)
    assert observed["busy_s"] == 8.0
    assert observed["observed_decode_tok_s"] == 60 / 8
    assert observed["observed_prefill_tok_s"] == 250 / 8
    assert observed["itl_mean_s"] == pytest.approx((0.1 * 9 + 0.2 * 19) / 28)
    assert observed["itl_gaps"] == 28
    assert observed["itl_max_s"] == 0.9
    assert observed["ttft_s"]["n"] == 2 and observed["e2e_s"]["n"] == 3
