"""release/perf.json and the report's "Inference performance" section."""

from __future__ import annotations

import json
from typing import Any

from evalspleef.control.batch.release import ReleaseInputs, build_release
from evalspleef.control.store.rows import BenchmarkRow, TrialRow
from evalspleef.core.events import EventEnvelope, EventType
from evalspleef.core.report import BatchReport

BATCH = "2026-11-b9"
ENDPOINT = "ep_demo_1234abcd"
TRIAL = "logic-001.demo.terminus.e1.t01"


def _request(ts: str, e2e_ms: float, output: int, **timing: Any) -> dict[str, Any]:
    return {
        "ts": ts,
        "endpoint_id": ENDPOINT,
        "status_code": 200,
        "input_tokens": 100,
        "cached_tokens": 40,
        "output_tokens": output,
        "ttft_ms": None,
        "e2e_ms": e2e_ms,
        "tpot_ms": None,
        "itl_mean_ms": None,
        "itl_p95_ms": None,
        "itl_max_ms": None,
        "itl_count": None,
        **timing,
    }


def _step(requests: list[dict[str, Any]], index: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt_{index}",
        ts="2026-11-09T00:00:00Z",
        batch_id=BATCH,
        type=EventType.AGENT_STEP,
        actor="runner",
        trial_id=TRIAL,
        payload={"round": 1, "input_tokens": 0, "output_tokens": 0, "gateway_requests": requests},
    )


def _inputs(**overrides: Any) -> ReleaseInputs:
    # One streamed request ending at :10 after 4 s, one non-streamed ending at :12 after 4 s:
    # busy time is the union [6, 12] = 6 s.
    streamed = _request(
        "2026-11-09T00:00:10+00:00",
        4000.0,
        30,
        ttft_ms=500.0,
        tpot_ms=20.0,
        itl_mean_ms=20.0,
        itl_p95_ms=40.0,
        itl_max_ms=90.0,
        itl_count=29,
    )
    plain = _request("2026-11-09T00:00:12+00:00", 4000.0, 30)
    trial = TrialRow(
        trial_id=TRIAL,
        batch_id=BATCH,
        cell="logic-001.demo.terminus",
        task_id="logic-001",
        model="demo",
        shell="terminus",
        state="scored",
        validity="valid",
        extensions={"versions": {"model_source": "vendor", "deployment_adapter": "oa"}},
        created_at="2026-11-09T00:00:00Z",
        updated_at="2026-11-09T00:00:00Z",
    )
    values: dict[str, Any] = {
        "report": BatchReport(batch_id=BATCH, trials=()),
        "trials": [trial],
        "grades": {},
        "events": [_step([streamed], 1), _step([plain], 2)],
        "freeze": {"probe_report_ids": {ENDPOINT: "pr_1"}},
        "probe_reports": {},
        "models": {},
        "judges": [],
        "costs": None,
    }
    values.update(overrides)
    return ReleaseInputs(**values)


def test_release_writes_perf_json_with_in_batch_distributions_from_the_event_log() -> None:
    files = build_release(_inputs())
    assert "perf.json" in files
    perf = json.loads(files["perf.json"])
    entry = perf["endpoints"][ENDPOINT]
    in_batch = entry["in_batch"]
    assert in_batch["label"] == "observed over the batch's busy windows, not saturated"
    assert (in_batch["requests"], in_batch["with_ttft"], in_batch["with_e2e"]) == (2, 1, 2)
    assert in_batch["ttft_s"]["p50"] == 0.5
    assert in_batch["e2e_s"]["n"] == 2 and in_batch["e2e_s"]["p50"] == 4.0
    assert in_batch["busy_s"] == 6.0
    assert in_batch["observed_decode_tok_s"] == 60 / 6
    assert in_batch["observed_prefill_tok_s"] == 120 / 6  # 2 x (100 - 40 cached)
    assert in_batch["itl_max_s"] == 0.09
    assert entry["benchmark"] is None
    assert entry["facts"]["serving"] == "api"
    assert "hosted API" in entry["note"]
    markdown = files["leaderboard.md"]
    assert "## Inference performance" in markdown
    assert f"### {ENDPOINT}" in markdown
    assert "No benchmark recorded for this endpoint" in markdown


def test_release_carries_the_latest_benchmark_with_its_facts() -> None:
    result = {
        "single": [],
        "sweep": [
            {
                "input_target": 2048,
                "output_target": 256,
                "levels": [],
                "saturation": {
                    "concurrency": 4,
                    "saturated": True,
                    "reason": "gain_below_threshold",
                    "next_gain": 0.008,
                },
                "full_load": {"system_decode_tok_s": 144.8, "system_prefill_tok_s": 1203.8},
                "usd_per_million_output_tokens": 8.9204,
            }
        ],
        "price_per_hour_usd": 4.65,
    }
    bench = BenchmarkRow(
        bench_id="bn_1",
        endpoint_id=ENDPOINT,
        created_at="2026-11-09T01:00:00Z",
        plan={},
        facts={"serving": "self-served", "gpu_type": "RTX5090", "gpu_count": 2, "note": "x"},
        result=result,
        raw_path="/state/bench/bn_1/raw.json",
        raw_sha256="sha256:ab",
    )
    files = build_release(_inputs(benchmarks={ENDPOINT: bench}))
    entry = json.loads(files["perf.json"])["endpoints"][ENDPOINT]
    assert entry["benchmark"]["bench_id"] == "bn_1"
    assert entry["benchmark"]["result"]["sweep"][0]["usd_per_million_output_tokens"] == 8.9204
    assert entry["facts"]["gpu_count"] == 2
    markdown = files["leaderboard.md"]
    assert "saturation at concurrency 4" in markdown
    assert "8.92 USD per million output tokens at 4.65 USD/h" in markdown
    assert "GPU RTX5090; GPUs 2" in markdown
