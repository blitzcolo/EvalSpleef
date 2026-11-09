"""spleef models bench against the fake OpenAI-compatible server with paced streaming."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.core.perf import bench_result
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer

FIRST_CHUNK_S = 0.05
CHUNK_S = 0.002


def test_bench_streams_through_the_gateway_and_stores_recomputable_raw_samples(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    tmp_path: Path,
    fake_server: FakeOpenAIServer,
    fake_admin: InMemoryGatewayAdmin,
) -> None:
    fake_server.first_chunk_delay_s = FIRST_CHUNK_S
    fake_server.chunk_interval_s = CHUNK_S
    fake_server.long_output_words = 8
    fake_server.cached_prompt_tokens = 5
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    probe_requests = len(fake_server.requests)

    result = cli_runner.invoke(
        app,
        [
            "models",
            "bench",
            "--model",
            "demo",
            "--shape",
            "64x8",
            "--repeats",
            "2",
            "--concurrency",
            "1,2",
            "--requests-per-worker",
            "2",
            "--seed",
            "11",
            "--price-per-hour",
            "3.6",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Single request (concurrency 1, unique prefixes)" in result.output
    assert "Concurrency sweep (64x8" in result.output

    (folder,) = (tmp_path / "state" / "bench").iterdir()
    raw = json.loads((folder / "raw.json").read_text(encoding="utf-8"))
    document = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert raw["plan"]["seed"] == 11 and raw["price_per_hour_usd"] == 3.6
    assert raw["prompt_generator"]["name"] == "ledger-v1"
    assert raw["gateway_trial_id"] == f"bench-{raw['bench_id']}"
    # 2 single requests, then 1 + 2 workers x 2 requests: the plan has only two levels.
    samples = raw["samples"]
    bench_requests = fake_server.requests[probe_requests:]
    assert len(samples) == 2 + 3 * 2 == len(bench_requests)
    assert all(request.body.get("stream") for request in bench_requests)
    first_lines = {str(r.body["messages"][0]["content"]).splitlines()[0] for r in bench_requests}
    assert len(first_lines) == len(bench_requests)  # unique prefixes, no cache sharing

    for sample in samples:
        assert sample["ok"], sample
        assert len(sample["chunk_times"]) == sample["output_tokens"] == 8
        assert sample["cached_tokens"] == 5
        # The server holds every first chunk back FIRST_CHUNK_S and paces the rest, so the
        # client cannot see them earlier; margins absorb event-loop clock resolution only.
        assert sample["chunk_times"][0] - sample["t_start"] >= FIRST_CHUNK_S * 0.9
        assert sample["t_end"] - sample["t_start"] >= FIRST_CHUNK_S * 0.9 + 7 * CHUNK_S * 0.9

    recomputed = bench_result(raw)
    assert {key: document[key] for key in recomputed} == recomputed
    (single,) = document["single"]
    assert single["metrics"]["with_ttft"] == 2
    assert single["metrics"]["ttft_s"]["p50"] >= FIRST_CHUNK_S * 0.9
    (sweep,) = document["sweep"]
    assert [level["concurrency"] for level in sweep["levels"]] == [1, 2]
    assert document["facts"]["serving"] == "api"
    assert document["facts"]["price_per_hour_usd"] == 3.6
    # The bench key (issued after the probe key) was revoked when the run ended.
    assert fake_admin.is_revoked("tk_2")


def test_bench_rejects_a_bad_shape(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = cli_runner.invoke(app, ["models", "bench", "--shape", "2048", *args])
    assert result.exit_code == 2
    assert "INPUTxOUTPUT" in result.output


def test_bench_refuses_an_endpoint_without_a_published_route(
    cli_runner: CliRunner, args: list[str], env: Path, fake_server: FakeOpenAIServer
) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["models", "bench", "--model", "demo", *args])
    assert result.exit_code == 1
    assert "spleef models probe" in result.output
    assert fake_server.requests == []  # no request reached the model
