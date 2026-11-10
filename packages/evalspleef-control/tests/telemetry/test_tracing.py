"""Telemetry tests: no-op default tracer, span wrapping of every SPI method.

Uses a real SDK tracer provider with the OpenTelemetry in-memory exporter;
spans are asserted by attribute, not by prose.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer

from evalspleef.control.telemetry import traced_provider, tracer
from evalspleef.control.telemetry.tracing import (
    TracedSandbox,
    TracedSandboxProvider,
)
from evalspleef.spi.model import (
    EndpointHandle,
    EndpointState,
    ModelEndpointProvider,
    ModelSpec,
)
from evalspleef.spi.sandbox import (
    ExecResult,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)

TRIAL = "web-001.demo.model.shell.e1.t01"


def make_spec() -> SandboxSpec:
    return SandboxSpec(
        trial_id=TRIAL,
        lease_id="lease-1",
        role="agent",
        os="linux",
        arch="x86_64",
        template=None,  # type: ignore[arg-type]
        resources=None,  # type: ignore[arg-type]
        network=None,  # type: ignore[arg-type]
    )


class FakeSandbox(Sandbox):
    def __init__(self, spec: SandboxSpec) -> None:
        self.id = "sb-1"
        self.spec = spec
        self.destroy_calls = 0

    async def exec(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
        return ExecResult(
            exit_code=0, stdout=b"hello ", stderr=b"world", timed_out=False, duration_s=0.1
        )

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        return None

    async def download(self, remote: str, local: Path, **kwargs) -> None:  # type: ignore[no-untyped-def]
        local.write_bytes(b"downloaded")  # noqa: ASYNC240 (fake double, not real I/O)
        return None

    async def destroy(self) -> None:
        self.destroy_calls += 1

    async def snapshot(self, name: str) -> str:
        return f"snap-{name}"


class FakeSandboxProvider(SandboxProvider):
    name = "fake-sandbox"

    def __init__(self) -> None:
        super().__init__("fake-instance", {}, None)  # type: ignore[arg-type]
        self.created: list[SandboxSpec] = []

    def capabilities(self) -> SandboxCapabilities:  # type: ignore[override]
        raise NotImplementedError

    def isolation_profile(self):  # type: ignore[override]
        raise NotImplementedError

    async def preflight(self):  # type: ignore[override]
        return {"ok": True}

    async def create(self, spec: SandboxSpec) -> Sandbox:
        self.created.append(spec)
        return FakeSandbox(spec)

    async def list(self, labels: Mapping[str, str]):  # type: ignore[override]
        return []

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:  # type: ignore[override]
        return 0


class FakeEndpointProvider(ModelEndpointProvider):
    name = "fake-model"
    management_mode = "api"  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__("fake-account", {}, None)  # type: ignore[arg-type]
        self.deployed: list[ModelSpec] = []

    async def validate(self, spec):  # type: ignore[override,no-untyped-def]
        return None

    async def plan(self, spec):  # type: ignore[override,no-untyped-def]
        return None

    async def deploy(self, spec, plan):  # type: ignore[override,no-untyped-def]
        self.deployed.append(spec)
        return EndpointHandle(
            endpoint_id="ep-1",
            model=spec,
            state=EndpointState.READY,
            base_url="http://example",
            upstream_model="m",
            protocol="chat_completions",
            credential_ref="vault://key",
        )

    async def status(self, handle):  # type: ignore[override,no-untyped-def]
        return EndpointState.READY

    async def wake(self, handle, timeout_s: int):  # type: ignore[override,no-untyped-def]
        return EndpointState.WARM

    async def teardown(self, handle, mode):  # type: ignore[override,no-untyped-def]
        return None

    def upstream(self, handle):  # type: ignore[override,no-untyped-def]
        return None


@pytest.fixture(scope="module")
def span_exporter() -> Iterator[InMemorySpanExporter]:
    """Configure the global SDK provider once, with an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture(autouse=True)
def _fresh_spans(span_exporter: InMemorySpanExporter) -> Iterator[None]:
    span_exporter.clear()
    yield


def spans_by_name(exporter: InMemorySpanExporter) -> dict[str, list]:  # type: ignore[type-arg]
    result: dict[str, list] = {}  # type: ignore[type-arg]
    for span in exporter.get_finished_spans():
        result.setdefault(span.name, []).append(span)
    return result


def test_tracer_is_noop_until_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from evalspleef.control.telemetry import tracing

    monkeypatch.setattr(
        tracing._trace, "get_tracer_provider", lambda: tracing._trace.ProxyTracerProvider()
    )
    assert isinstance(tracer(), NoOpTracer)


def test_tracer_emits_once_configured(span_exporter: InMemorySpanExporter) -> None:
    with tracer().start_as_current_span("probe") as span:
        span.set_attribute("op", "probe")
    spans = spans_by_name(span_exporter)
    assert "probe" in spans
    assert spans["probe"][0].attributes["op"] == "probe"


async def test_traced_sandbox_provider_wraps_every_method(
    span_exporter: InMemorySpanExporter,
) -> None:
    provider = FakeSandboxProvider()
    traced = traced_provider(provider)
    assert isinstance(traced, TracedSandboxProvider)

    sandbox = await traced.create(make_spec())
    assert isinstance(sandbox, TracedSandbox)
    await sandbox.exec(["ls"])
    await sandbox.snapshot("pre-feedback")  # optional capability still delegates
    await sandbox.destroy()
    assert provider.created[0].trial_id == TRIAL

    spans = spans_by_name(span_exporter)
    create = spans["create"][0]
    assert create.attributes["op"] == "create"
    assert create.attributes["provider_instance"] == "fake-instance"
    assert create.attributes["trial_id"] == TRIAL
    assert create.attributes["sandbox_id"] == "sb-1"

    exec_span = spans["exec"][0]
    assert exec_span.attributes["trial_id"] == TRIAL
    assert exec_span.attributes["bytes"] == len(b"hello ") + len(b"world")
    assert exec_span.attributes["exit_code"] == 0
    assert exec_span.attributes["timed_out"] is False

    assert "snapshot" in spans  # optional SPI methods are wrapped too
    assert "destroy" in spans


async def test_traced_sandbox_upload_download_record_bytes(
    span_exporter: InMemorySpanExporter, tmp_path: Path
) -> None:
    local_file = tmp_path / "payload.bin"
    local_file.write_bytes(b"x" * 123)
    target = tmp_path / "out.bin"

    sandbox = traced_provider(FakeSandbox(make_spec()))
    await sandbox.upload(local_file, "/remote/payload.bin")
    await sandbox.download("/remote/result", target)

    spans = spans_by_name(span_exporter)
    assert spans["upload"][0].attributes["bytes"] == 123
    assert spans["download"][0].attributes["bytes"] == len(b"downloaded")


async def test_traced_endpoint_provider_wraps_every_method(
    span_exporter: InMemorySpanExporter,
) -> None:
    provider = FakeEndpointProvider()
    traced = traced_provider(provider)

    handle = await traced.deploy(None, None)  # type: ignore[arg-type]
    await traced.status(handle)
    await traced.wake(handle, timeout_s=1)
    await traced.teardown(handle, "terminate")  # type: ignore[arg-type]
    traced.upstream(handle)

    spans = spans_by_name(span_exporter)
    deploy = spans["deploy"][0]
    assert deploy.attributes["op"] == "deploy"
    assert deploy.attributes["provider_instance"] == "fake-account"
    assert deploy.attributes["endpoint_id"] == "ep-1"
    # Endpoint handles carry no trial id; the attribute is absent, not empty.
    assert "trial_id" not in deploy.attributes
    for name in ("status", "wake", "teardown", "upstream"):
        assert name in spans


def test_traced_provider_rejects_unknown_types() -> None:
    with pytest.raises(TypeError, match="unsupported provider type"):
        traced_provider(object())  # type: ignore[arg-type]
