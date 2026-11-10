"""OpenTelemetry tracing helpers for SPI calls (TECH-DESIGN section 12).

Every sandbox and endpoint provider call is one span carrying the attributes
``trial_id``, ``provider_instance``, ``op``, ``bytes``, ``exit_code`` and
``timed_out`` (the ones that apply to the operation). :func:`tracer` returns a
no-op tracer until an application configures a real SDK provider, so importing
the control plane never requires an OTel setup.

Usage::

    traced = traced_provider(provider)          # any SandboxProvider/ModelEndpointProvider/Sandbox
    sandbox = await traced.create(spec)         # spans are created and exported automatically
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, overload

from opentelemetry import trace as _trace
from opentelemetry.trace import Tracer

from evalspleef.spi.environment import ExecutionEnvironment
from evalspleef.spi.model import (
    BillLine,
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    EngineLog,
    ModelEndpointProvider,
    ModelSpec,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.sandbox import (
    ExecResult,
    NetworkPolicy,
    OutputCallback,
    Sandbox,
    SandboxProvider,
    SandboxSpec,
)

__all__ = ["traced_provider", "tracer"]

_DEFAULT_TRACER_NAME = "evalspleef.control"


def tracer(name: str = _DEFAULT_TRACER_NAME, version: str = "") -> Tracer:
    """Return a tracer for ``name``; no-op until OpenTelemetry is configured.

    The global provider is a proxy/no-op placeholder until the application
    calls ``trace.set_tracer_provider`` with an SDK provider; only then do
    spans become real, recording spans.
    """
    provider = _trace.get_tracer_provider()
    if isinstance(provider, _trace.ProxyTracerProvider | _trace.NoOpTracerProvider):
        return _trace.NoOpTracer()
    return provider.get_tracer(name, version)


_default_tracer = tracer


def _provider_instance(provider: Any) -> str | None:
    """Identify the configured provider instance across the SPI types."""
    for attribute in ("instance", "account"):
        value = getattr(provider, attribute, None)
        if isinstance(value, str) and value:
            return value
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) else None


def _trial_id_of(*args: Any) -> str | None:
    """Find a trial id in the call arguments (spec/handle/sandbox)."""
    for arg in args:
        if isinstance(arg, SandboxSpec):
            return arg.trial_id
        if isinstance(arg, Sandbox):
            return arg.spec.trial_id
    return None


def _result_attributes(result: Any) -> dict[str, Any]:
    """Span attributes describing an SPI call result."""
    attributes: dict[str, Any] = {}
    if isinstance(result, ExecResult):
        attributes["bytes"] = len(result.stdout) + len(result.stderr)
        attributes["exit_code"] = result.exit_code
        attributes["timed_out"] = result.timed_out
    return attributes


def _upload_bytes(local: Path) -> int | None:
    return local.stat().st_size if local.is_file() else None


def _download_bytes(local: Path) -> int | None:
    try:
        return local.stat().st_size
    except OSError:
        return None


class _SpanMixin:
    """Span-lifecycle helpers shared by the traced wrappers."""

    _tracer: Tracer | None
    _provider_instance: str | None

    def _start_span(self, op: str, *args: Any) -> AbstractContextManager[Any]:
        attributes: dict[str, Any] = {"op": op}
        if self._provider_instance is not None:
            attributes["provider_instance"] = self._provider_instance
        trial_id = _trial_id_of(*args)
        if trial_id is not None:
            attributes["trial_id"] = trial_id
        # Resolved per call, not at wrap time: providers are wrapped when the registry is
        # built, which may be before (or without) an SDK provider being installed.
        active = self._tracer if self._tracer is not None else _default_tracer()
        return active.start_as_current_span(op, attributes=attributes)

    @staticmethod
    def _record_result(span: Any, result: Any, *, extra: Mapping[str, Any] | None = None) -> None:
        attributes = _result_attributes(result)
        if extra:
            attributes.update({key: value for key, value in extra.items() if value is not None})
        span.set_attributes(attributes)


class TracedSandbox(_SpanMixin, Sandbox):
    """Sandbox with every SPI call wrapped in a span."""

    def __init__(
        self,
        inner: Sandbox,
        *,
        tracer: Tracer | None = None,
        provider_instance: str | None = None,
    ) -> None:
        self._inner = inner
        self._tracer = tracer
        self._provider_instance = (
            provider_instance if provider_instance is not None else _provider_instance(inner)
        )
        self.id = inner.id
        self.spec = inner.spec

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        with self._start_span("exec", self._inner) as span:
            result = await self._inner.exec(
                cmd,
                cwd=cwd,
                env=env,
                user=user,
                timeout_s=timeout_s,
                stdin=stdin,
                on_output=on_output,
                max_output_bytes=max_output_bytes,
            )
            self._record_result(span, result)
            return result

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        with self._start_span("upload", self._inner) as span:
            await self._inner.upload(local, remote, read_only=read_only)
            self._record_result(span, None, extra={"bytes": _upload_bytes(local)})

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        with self._start_span("download", self._inner) as span:
            await self._inner.download(remote, local, max_bytes=max_bytes)
            self._record_result(span, None, extra={"bytes": _download_bytes(local)})

    async def destroy(self) -> None:
        with self._start_span("destroy", self._inner):
            await self._inner.destroy()

    async def snapshot(self, name: str) -> str:
        with self._start_span("snapshot", self._inner):
            return await self._inner.snapshot(name)

    async def set_network(self, policy: NetworkPolicy) -> None:
        with self._start_span("set_network", self._inner):
            await self._inner.set_network(policy)

    async def gui_endpoint(self) -> str:
        with self._start_span("gui_endpoint", self._inner):
            return await self._inner.gui_endpoint()

    async def expose_port(self, port: int) -> str:
        with self._start_span("expose_port", self._inner):
            return await self._inner.expose_port(port)

    async def describe(self) -> dict[str, Any]:
        with self._start_span("describe", self._inner):
            return await self._inner.describe()


class TracedSandboxProvider(_SpanMixin, SandboxProvider):
    """SandboxProvider with every SPI method wrapped in a span."""

    def __init__(self, inner: SandboxProvider, *, tracer: Tracer | None = None) -> None:
        super().__init__(inner.instance, inner.config, inner.secrets)
        self._inner = inner
        self._tracer = tracer
        self._provider_instance = _provider_instance(inner)
        self.name = inner.name  # the adapter name recorded in trial.versions

    async def close(self) -> None:
        """Release adapter resources; the wrapper must not hide the adapter's own close."""
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()

    def capabilities(self) -> Any:
        with self._start_span("capabilities", self._inner):
            return self._inner.capabilities()

    def isolation_profile(self) -> Any:
        with self._start_span("isolation_profile", self._inner):
            return self._inner.isolation_profile()

    async def preflight(self) -> dict[str, Any]:
        with self._start_span("preflight", self._inner):
            return await self._inner.preflight()

    async def environment(self) -> ExecutionEnvironment:
        with self._start_span("environment", self._inner):
            return await self._inner.environment()

    async def create(self, spec: SandboxSpec) -> Sandbox:
        with self._start_span("create", spec) as span:
            sandbox = await self._inner.create(spec)
            span.set_attribute("sandbox_id", sandbox.id)
            return TracedSandbox(
                sandbox, tracer=self._tracer, provider_instance=self._provider_instance
            )

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        with self._start_span("create_from_snapshot", spec) as span:
            sandbox = await self._inner.create_from_snapshot(snapshot_id, spec)
            span.set_attribute("sandbox_id", sandbox.id)
            return TracedSandbox(
                sandbox, tracer=self._tracer, provider_instance=self._provider_instance
            )

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        with self._start_span("list", self._inner):
            return await self._inner.list(labels)

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        with self._start_span("reap", self._inner):
            return await self._inner.reap(labels, older_than_s)


class TracedEndpointProvider(_SpanMixin, ModelEndpointProvider):
    """ModelEndpointProvider with every SPI method wrapped in a span."""

    def __init__(self, inner: ModelEndpointProvider, *, tracer: Tracer | None = None) -> None:
        super().__init__(inner.account, inner.config, inner.secrets)
        self._inner = inner
        self._tracer = tracer
        self._provider_instance = _provider_instance(inner)
        self.name = inner.name
        self.management_mode = inner.management_mode
        self.supports_billing = inner.supports_billing

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        with self._start_span("validate", self._inner):
            return await self._inner.validate(spec)

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        with self._start_span("plan", self._inner):
            return await self._inner.plan(spec)

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        with self._start_span("deploy", self._inner) as span:
            handle = await self._inner.deploy(spec, plan)
            span.set_attribute("endpoint_id", handle.endpoint_id)
            return handle

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        with self._start_span("attach", self._inner):
            return await self._inner.attach(handle, operator_input)

    async def status(self, handle: EndpointHandle) -> EndpointState:
        with self._start_span("status", self._inner):
            return await self._inner.status(handle)

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        with self._start_span("scale", self._inner):
            await self._inner.scale(handle, min_replicas, max_replicas)

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        with self._start_span("wake", self._inner):
            return await self._inner.wake(handle, timeout_s)

    async def engine_log(self, handle: EndpointHandle, *, max_bytes: int) -> EngineLog:
        with self._start_span("engine_log", self._inner):
            return await self._inner.engine_log(handle, max_bytes=max_bytes)

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        with self._start_span("teardown", self._inner):
            await self._inner.teardown(handle, mode)

    def parse_bill(self, text: str) -> Sequence[BillLine]:
        with self._start_span("parse_bill", self._inner):
            return self._inner.parse_bill(text)

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        with self._start_span("upstream", self._inner):
            return self._inner.upstream(handle)


@overload
def traced_provider(
    provider: SandboxProvider, *, tracer: Tracer | None = None
) -> SandboxProvider: ...


@overload
def traced_provider(
    provider: ModelEndpointProvider, *, tracer: Tracer | None = None
) -> ModelEndpointProvider: ...


@overload
def traced_provider(provider: Sandbox, *, tracer: Tracer | None = None) -> Sandbox: ...


def traced_provider(
    provider: SandboxProvider | ModelEndpointProvider | Sandbox,
    *,
    tracer: Tracer | None = None,
) -> SandboxProvider | ModelEndpointProvider | Sandbox:
    """Wrap every SPI method of ``provider`` in an OpenTelemetry span.

    Accepts a :class:`~evalspleef.spi.sandbox.SandboxProvider`, a
    :class:`~evalspleef.spi.model.ModelEndpointProvider` or a live
    :class:`~evalspleef.spi.sandbox.Sandbox`. Sandboxes returned by a traced
    provider's ``create`` are traced as well. Pass ``tracer`` to use a specific
    tracer instead of the module default (see :func:`tracer`).
    """
    if isinstance(provider, Sandbox):
        return TracedSandbox(provider, tracer=tracer)
    if isinstance(provider, SandboxProvider):
        return TracedSandboxProvider(provider, tracer=tracer)
    if isinstance(provider, ModelEndpointProvider):
        return TracedEndpointProvider(provider, tracer=tracer)
    raise TypeError(f"unsupported provider type: {type(provider).__name__}")
