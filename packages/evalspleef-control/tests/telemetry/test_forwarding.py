"""The traced wrappers must forward everything the control plane reads from a provider.

A delegating wrapper once dropped ``name`` and made every real trial invalid while the
mock-sandbox suites stayed green. The attribute lists below come from the consumers:
the registry/lease/runner code (``instance``, ``secrets``, ``capabilities()``,
``isolation_profile()``, ``preflight()``, ``create()``, ``list()``, ``reap()``,
``close()``, sandbox ``id``/``spec``/``exec``/``upload``/``download``/``destroy``),
``_BridgedProvider`` (``instance``, ``secrets``, ``name``) and the endpoint manager and
CLI (``name``, ``management_mode``, ``account``, the lifecycle methods).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from evalspleef.control.telemetry import traced_provider
from evalspleef.control.telemetry.tracing import (
    TracedEndpointProvider,
    TracedSandbox,
    TracedSandboxProvider,
)
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.model import (
    DeploymentSpec,
    EndpointState,
    HFSource,
    ModelSpec,
)
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Resources,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.testing.mock_endpoint import MockAssistedProvider
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

TRIAL = "logic-001.demo.model.shell.e1.t01"


class _Recorder:
    """A private SDK tracer provider (no global state) with an in-memory exporter."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer: Tracer = provider.get_tracer("test")

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def ops(self) -> set[str]:
        return {str((span.attributes or {})["op"]) for span in self.spans()}


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    yield _Recorder()


def _sandbox_spec(trial_id: str = TRIAL) -> SandboxSpec:
    return SandboxSpec(
        trial_id=trial_id,
        lease_id="lease-1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="registry.test/x:1", digest="sha256:" + "a" * 64),
        resources=Resources(cpus=1, memory_mb=256, disk_mb=512),
        network=NetworkPolicy(mode=NetMode.OFF),
    )


class _ClosingMock(MockSandboxProvider):
    """A mock sandbox provider with an adapter-level ``close`` like real adapters."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_sandbox_provider_forwards_everything_the_control_plane_reads(
    recorder: _Recorder, tmp_path: Path
) -> None:
    secrets = DictSecretResolver()
    inner = _ClosingMock("docker-a", {"k": "v"}, secrets)
    traced = traced_provider(inner, tracer=recorder.tracer)
    assert isinstance(traced, TracedSandboxProvider)

    # plain attributes
    assert traced.name == inner.name
    assert traced.instance == inner.instance
    assert traced.secrets is inner.secrets
    assert dict(traced.config) == dict(inner.config)
    # declared values
    assert traced.capabilities() is inner.capabilities()
    assert traced.isolation_profile() == inner.isolation_profile()
    # management methods
    assert (await traced.preflight())["instance"] == "docker-a"
    assert await traced.list({}) == await inner.list({})
    assert await traced.reap({}, 3600) == 0
    await traced.close()  # type: ignore[attr-defined]
    assert inner.closed == 1

    # the sandbox returned by create is wrapped and forwards its own surface
    sandbox = await traced.create(_sandbox_spec())
    assert isinstance(sandbox, TracedSandbox)
    created = inner.created_sandboxes()[0]
    assert sandbox.id == created.id
    assert sandbox.spec == created.spec
    result = await sandbox.exec(["echo", "hi"])
    assert result.exit_code == 0
    assert result.stdout.strip() == b"hi"
    source = tmp_path / "in.txt"
    source.write_bytes(b"payload")
    await sandbox.upload(source, "/workspace/in.txt")
    target = tmp_path / "out.txt"
    await sandbox.download("/workspace/in.txt", target)
    assert target.read_bytes() == b"payload"
    assert (await sandbox.describe())["template_digest"] == created.spec.template.digest
    await sandbox.destroy()
    assert created.destroyed

    assert {"create", "exec", "upload", "download", "destroy", "describe"} <= recorder.ops()
    assert {"capabilities", "isolation_profile", "preflight", "list", "reap"} <= recorder.ops()


def _model_spec() -> ModelSpec:
    return ModelSpec(
        slug="mock-32b",
        spec_version=1,
        family="mock",
        roles=frozenset({"target"}),
        source=HFSource(repo="org/mock-32b", revision="0" * 40, access="public"),
        deployment=DeploymentSpec(adapter="mock-assisted", account="acct", params={}),
        declared_context_tokens=32768,
        declared_max_output_tokens=4096,
        pricing_ref="ref:mock/32b",
        license="apache-2.0",
        owner="qa",
    )


async def test_endpoint_provider_forwards_everything_the_control_plane_reads(
    recorder: _Recorder,
) -> None:
    inner = MockAssistedProvider(
        "acct",
        {"model": "mock-32b-upstream", "api_key_ref": "vault://m/k"},
        DictSecretResolver(),
    )
    traced = traced_provider(inner, tracer=recorder.tracer)
    assert isinstance(traced, TracedEndpointProvider)

    assert traced.name == inner.name
    assert traced.account == inner.account
    assert traced.management_mode is inner.management_mode
    assert traced.secrets is inner.secrets
    assert dict(traced.config) == dict(inner.config)

    spec = _model_spec()
    assert (await traced.validate(spec)) == (await inner.validate(spec))
    plan = await traced.plan(spec)
    handle = await traced.deploy(spec, plan)
    assert handle.state is EndpointState.AWAITING_OPERATOR
    handle = await traced.attach(
        handle, {"base_url": "https://api.example.test/v1", "model": "mock-32b-upstream"}
    )
    assert await traced.status(handle) is EndpointState.VERIFYING
    await traced.scale(handle, 0, 1)
    await traced.wake(handle, 1)
    assert traced.upstream(handle).api_base == "https://api.example.test/v1"
    await traced.teardown(handle, "retire")  # type: ignore[arg-type]

    assert {
        "validate",
        "plan",
        "deploy",
        "attach",
        "status",
        "scale",
        "wake",
        "upstream",
        "teardown",
    } <= recorder.ops()
    # every span names the account, never a trial
    assert all((span.attributes or {})["provider_instance"] == "acct" for span in recorder.spans())


async def test_one_trial_id_joins_create_exec_upload_destroy(
    recorder: _Recorder, tmp_path: Path
) -> None:
    inner = MockSandboxProvider("docker-a", {}, DictSecretResolver())
    traced = traced_provider(inner, tracer=recorder.tracer)
    other = _sandbox_spec("logic-001.demo.model.shell.e1.t02")
    sandbox = await traced.create(_sandbox_spec())
    other_sandbox = await traced.create(other)
    source = tmp_path / "f.txt"
    source.write_bytes(b"x")
    await sandbox.upload(source, "/workspace/f.txt")
    await sandbox.exec(["true"])
    await sandbox.destroy()
    await other_sandbox.destroy()

    by_trial: dict[str, set[str]] = {}
    for span in recorder.spans():
        attributes = span.attributes or {}
        if "trial_id" in attributes:
            by_trial.setdefault(str(attributes["trial_id"]), set()).add(str(attributes["op"]))
    assert by_trial[TRIAL] == {"create", "exec", "upload", "destroy"}
    assert by_trial[other.trial_id] == {"create", "destroy"}
    assert all(
        (span.attributes or {}).get("provider_instance") == "docker-a"
        for span in recorder.spans()
        if (span.attributes or {}).get("op") in {"create", "exec", "upload", "destroy"}
    )
