"""NFR-10 on real Docker: every SPI call of a batch is a span, joinable per trial.

A local OpenTelemetry collector (the image the compose stack pins, with the file
exporter of ``deploy/compose/otel-collector.yaml``) receives the spans the real
``spleef`` CLI exports over OTLP/HTTP while it runs a batch on local Docker. The
assertions read the collector's JSON lines: every trial of the batch must have spans
for create, exec, upload and destroy, each with ``op`` and ``provider_instance``.

The batch runs on a fresh store and event log (the scenario of
``test_batch_scenario`` owns the session store) through the same session fixtures:
gateway, fake model server, configuration and CLI harness.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.111.0"  # pinned by deploy/compose
COLLECTOR_CONFIG = REPO / "deploy" / "compose" / "otel-collector.yaml"
CONTAINER_PREFIX = "spleef-tracelane-"
REQUIRED_OPS = frozenset({"create", "exec", "upload", "destroy"})
EXPORT_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class Collector:
    endpoint: str
    output: Path


@dataclass(frozen=True)
class ExportedSpan:
    name: str
    resource: Mapping[str, str]
    attributes: Mapping[str, Any]


def _docker(*argv: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )


def _derived_collector_config() -> dict[str, Any]:
    """The compose collector config, reduced to the traces pipeline into the file exporter."""
    config = yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    config["exporters"]["file"]["path"] = "/var/log/evalspleef/traces.jsonl"
    config["service"]["pipelines"] = {
        "traces": config["service"]["pipelines"]["traces"],
    }
    return config  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def collector(tmp_path_factory: pytest.TempPathFactory, docker_ready: None) -> Iterator[Collector]:
    del docker_ready
    work = tmp_path_factory.mktemp("otel")
    config_dir, out_dir = work / "config", work / "out"
    config_dir.mkdir()
    out_dir.mkdir()
    out_dir.chmod(0o777)  # the collector image runs as a non-root user
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(_derived_collector_config()), encoding="utf-8"
    )
    if _docker("image", "inspect", COLLECTOR_IMAGE).returncode != 0:
        pulled = _docker("pull", COLLECTOR_IMAGE, timeout=600)
        if pulled.returncode != 0:
            pytest.fail(f"cannot pull {COLLECTOR_IMAGE}: {pulled.stderr.strip()[-500:]}")
    name = f"{CONTAINER_PREFIX}otelcol-{os.getpid()}"
    started = _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "spleef-tracelane=1",
        "-p",
        "127.0.0.1::4318",
        "-v",
        f"{config_dir}:/etc/otelcol-contrib:ro",
        "-v",
        f"{out_dir}:/var/log/evalspleef",
        COLLECTOR_IMAGE,
    )
    if started.returncode != 0:
        pytest.fail(f"cannot start the collector: {started.stderr.strip()[-500:]}")
    try:
        deadline = time.monotonic() + 60
        while "Everything is ready" not in (logs := _docker("logs", name)).stdout + logs.stderr:
            if time.monotonic() > deadline:
                pytest.fail(f"the collector was not ready in 60s:\n{logs.stderr[-1500:]}")
            time.sleep(0.2)
        mapped = _docker("port", name, "4318/tcp").stdout.strip().splitlines()[0]
        yield Collector(
            endpoint=f"http://127.0.0.1:{mapped.rsplit(':', 1)[1]}",
            output=out_dir / "traces.jsonl",
        )
    finally:
        _docker("rm", "-f", name)


def _attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for item in items:
        (kind, value), *_ = item["value"].items()
        values[item["key"]] = int(value) if kind == "intValue" else value
    return values


def _exported_spans(output: Path) -> list[ExportedSpan]:
    if not output.is_file():
        return []
    spans: list[ExportedSpan] = []
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for resource_spans in json.loads(line).get("resourceSpans", []):
            resource = _attributes(resource_spans.get("resource", {}).get("attributes", []))
            for scope in resource_spans.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans.append(
                        ExportedSpan(
                            span["name"], resource, _attributes(span.get("attributes", []))
                        )
                    )
    return spans


def _trial_ids(store: Path) -> set[str]:
    connection = sqlite3.connect(store)
    try:
        return {row[0] for row in connection.execute("SELECT trial_id FROM trial")}
    finally:
        connection.close()


def _ops_by_trial(spans: list[ExportedSpan], run: str) -> dict[str, set[str]]:
    ops: dict[str, set[str]] = {}
    for span in spans:
        if span.resource.get("e2e.run") != run or "trial_id" not in span.attributes:
            continue
        op = span.attributes.get("op")
        if op is None:  # runner spans carry trial_id but no SPI op
            continue
        ops.setdefault(str(span.attributes["trial_id"]), set()).add(str(op))
    return ops


def _await_export(collector: Collector, expected: set[str]) -> list[ExportedSpan]:
    """Wait (bounded) until every expected trial has its required ops in the output file."""
    deadline = time.monotonic() + EXPORT_TIMEOUT_S
    while True:
        spans = _exported_spans(collector.output)
        ops = _ops_by_trial(spans, "traced")
        if all(ops.get(trial, set()) >= REQUIRED_OPS for trial in expected):
            return spans
        if time.monotonic() > deadline:
            return spans
        time.sleep(0.5)


@dataclass(frozen=True)
class TracedRun:
    spans: list[ExportedSpan]
    trials: set[str]


@pytest.fixture(scope="module")
def traced_run(
    e2e: Any, collector: Collector, tmp_path_factory: pytest.TempPathFactory
) -> TracedRun:
    work = tmp_path_factory.mktemp("traced-batch")
    base_env = {k: v for k, v in e2e.env.items() if not k.startswith("OTEL_")}
    paths = {
        "SPLEEF_STORE_URL": f"sqlite:///{work / 'store.sqlite3'}",
        "SPLEEF_EVENTS_URL": f"jsonl://{work / 'events'}",
        "SPLEEF_STATE_DIR": str(work / "state"),
    }
    harness = dataclasses.replace(
        e2e,
        env=base_env | paths | {"OTEL_RESOURCE_ATTRIBUTES": "e2e.run=untraced"},
        work=work,
        store_path=work / "store.sqlite3",
        events_dir=work / "events",
        counter=e2e.counter + 100,
    )

    # WITHOUT the endpoint variable: provider calls happen but nothing may be exported.
    result = harness.run_cli(
        "trace-register", "models", "register", *harness.options(with_batch=False)
    )
    assert result.returncode == 0, result.stderr[-2000:]
    result = harness.run_cli(
        "trace-probe-untraced", "models", "probe", *harness.options(with_batch=False)
    )
    assert result.returncode == 0, result.stderr[-2000:]

    # WITH the endpoint variable: the whole batch is traced.
    traced = dataclasses.replace(
        harness,
        env=harness.env
        | {
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
            "OTEL_RESOURCE_ATTRIBUTES": "e2e.run=traced",
        },
    )
    for step in ("plan", "freeze", "run"):
        result = traced.run_cli(f"trace-batch-{step}", "batch", step, *traced.options())
        assert result.returncode == 0, result.stderr[-2000:]
    trials = _trial_ids(traced.store_path)
    assert trials, "the batch produced no trials"
    return TracedRun(spans=_await_export(collector, trials), trials=trials)


@pytest.mark.e2e
def test_every_trial_has_create_exec_upload_destroy_spans(traced_run: TracedRun) -> None:
    ops = _ops_by_trial(traced_run.spans, "traced")
    assert set(ops) >= traced_run.trials, f"trials without any span: {traced_run.trials - set(ops)}"
    for trial in sorted(traced_run.trials):
        assert ops[trial] >= REQUIRED_OPS, f"{trial}: missing {REQUIRED_OPS - ops[trial]}"


@pytest.mark.e2e
def test_spans_carry_service_op_and_provider_instance(traced_run: TracedRun) -> None:
    sandbox_spans = [
        span
        for span in traced_run.spans
        if span.resource.get("e2e.run") == "traced"
        and span.attributes.get("op") in REQUIRED_OPS
        and "trial_id" in span.attributes
    ]
    assert sandbox_spans
    for span in sandbox_spans:
        assert span.resource["service.name"] == "spleef-control"
        assert span.name == span.attributes["op"]
        assert span.attributes["provider_instance"] == "local-docker"
    execs = [span for span in sandbox_spans if span.attributes["op"] == "exec"]
    assert all("exit_code" in span.attributes and "timed_out" in span.attributes for span in execs)


@pytest.mark.e2e
def test_run_without_endpoint_exports_nothing(traced_run: TracedRun) -> None:
    # The untraced run finished before the traced batch started and the collector pipeline
    # is ordered, so any span it exported would be in the file once the batch's spans are.
    assert traced_run.spans
    assert [s for s in traced_run.spans if s.resource.get("e2e.run") == "untraced"] == []
