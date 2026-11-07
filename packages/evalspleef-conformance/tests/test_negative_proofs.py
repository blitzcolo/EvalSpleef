"""Negative proofs: a conformance case must be able to fail.

Each broken provider below wraps the mock sandbox and breaks exactly one behaviour. The
matching case has to FAIL against the broken provider and PASS against its control (the
unbroken mock, or a provider that enforces the policy the broken one ignores), so a red
result is caused by the defect and not by the harness. The builtin mock targets must pass
their own full case set.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

import evalspleef.conformance.plugin as plugin
from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.errors import SandboxLost
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    ExecResult,
    NetworkPolicy,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

TIMEOUT_GUARD_S = 1.5
"""Cap the timeout-ignoring double puts on a command so the test finishes; a real provider with
this defect would simply block until the command ends."""

BLACKHOLE_PROXY = "http://127.0.0.1:9"
"""Loopback discard port: nothing listens, so any request routed through it is refused."""


# -- delegating doubles ---------------------------------------------------------------------


class DelegatingSandbox(Sandbox):
    """Forwards everything to the wrapped sandbox; subclasses break one method."""

    def __init__(self, inner: Sandbox, spec: SandboxSpec | None = None) -> None:
        self.inner = inner
        self.id = inner.id
        self.spec = spec or inner.spec

    async def exec(self, cmd: str | list[str], **kwargs: Any) -> ExecResult:
        return await self.inner.exec(cmd, **kwargs)

    async def upload(self, local: Path, remote: str) -> None:
        await self.inner.upload(local, remote)

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        await self.inner.download(remote, local, max_bytes=max_bytes)

    async def destroy(self) -> None:
        await self.inner.destroy()


class DelegatingProvider(SandboxProvider):
    """Forwards everything to a mock provider; subclasses break one behaviour."""

    name = "broken-double"

    def __init__(self, inner: MockSandboxProvider) -> None:
        super().__init__(inner.instance, inner.config, inner.secrets)
        self.inner = inner

    def capabilities(self) -> SandboxCapabilities:
        return self.inner.capabilities()

    def isolation_profile(self) -> IsolationProfile:
        return self.inner.isolation_profile()

    async def preflight(self) -> dict[str, Any]:
        return await self.inner.preflight()

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await self.inner.create(spec)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        return await self.inner.list(labels)

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        return await self.inner.reap(labels, older_than_s)


class TimeoutIgnoringSandbox(DelegatingSandbox):
    """Never honours the caller's timeout and never reports one."""

    async def exec(self, cmd: str | list[str], **kwargs: Any) -> ExecResult:
        if kwargs.get("timeout_s") is not None:
            kwargs["timeout_s"] = TIMEOUT_GUARD_S
        result = await self.inner.exec(cmd, **kwargs)
        if result.timed_out:
            return replace(result, timed_out=False, exit_code=137)
        return result


class MislabelingSandbox(DelegatingSandbox):
    """Reports every non-timeout run as exit code 0."""

    async def exec(self, cmd: str | list[str], **kwargs: Any) -> ExecResult:
        result = await self.inner.exec(cmd, **kwargs)
        return result if result.timed_out else replace(result, exit_code=0)


class RepeatDestroyFailsSandbox(DelegatingSandbox):
    """Destroy raises when the instance is already gone instead of succeeding."""

    def __init__(self, inner: Sandbox, spec: SandboxSpec | None = None) -> None:
        super().__init__(inner, spec)
        self._destroyed = False

    async def destroy(self) -> None:
        if self._destroyed:
            raise SandboxLost(f"sandbox {self.id} already destroyed")
        self._destroyed = True
        await self.inner.destroy()


class RetainedSandbox(DelegatingSandbox):
    """A fresh-looking handle whose destroy leaves the shared instance and its files alone."""

    async def destroy(self) -> None:
        return None


class FaultyProvider(DelegatingProvider):
    """Wraps every created sandbox with ``sandbox_type``."""

    def __init__(
        self, inner: MockSandboxProvider, sandbox_type: Callable[[Sandbox], Sandbox]
    ) -> None:
        super().__init__(inner)
        self._sandbox_type = sandbox_type

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return self._sandbox_type(await self.inner.create(spec))


class SharedRootProvider(DelegatingProvider):
    """Hands every create the same underlying instance, so files leak between sandboxes."""

    def __init__(self, inner: MockSandboxProvider) -> None:
        super().__init__(inner)
        self._shared: Sandbox | None = None

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._shared is None:
            self._shared = await self.inner.create(spec)
        return RetainedSandbox(self._shared, spec)

    async def close(self) -> None:
        if self._shared is not None:
            await self._shared.destroy()


class OffModeProxyProvider(DelegatingProvider):
    """Enforces ``NetMode.OFF`` by routing HTTP through a refusing proxy (the control)."""

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if spec.network.mode.value == "off":
            env = {**spec.env, "http_proxy": BLACKHOLE_PROXY, "all_proxy": BLACKHOLE_PROXY}
            spec = replace(spec, env=env, network=NetworkPolicy(mode=spec.network.mode))
        return await self.inner.create(spec)


@dataclass(frozen=True)
class ObservedTarget(ConformanceTarget):
    """A target that tells the network cases where the sandbox can see this process."""

    reachable_from_sandbox: str = ""


# -- target builders ------------------------------------------------------------------------


def _mock_provider() -> MockSandboxProvider:
    return MockSandboxProvider("negative-proof", {}, DictSecretResolver())


def _target(
    name: str,
    provider: SandboxProvider,
    *,
    cleanup: Callable[[], Any] | None = None,
    observer: str = "",
) -> ConformanceTarget:
    (builtin,) = [target for target in build_targets() if target.name == "mock-sandbox"]
    return ObservedTarget(
        name=name,
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=builtin.spec_factory,
        cleanup=cleanup,
        reachable_from_sandbox=observer,
    )


def _plain_mock() -> ConformanceTarget:
    return _target("control-mock", _mock_provider())


def _timeout_ignoring() -> ConformanceTarget:
    return _target("ignores-timeouts", FaultyProvider(_mock_provider(), TimeoutIgnoringSandbox))


def _marker_leaking() -> ConformanceTarget:
    provider = SharedRootProvider(_mock_provider())
    return _target("leaks-marker", provider, cleanup=provider.close)


def _destroy_not_idempotent() -> ConformanceTarget:
    return _target(
        "destroy-not-idempotent", FaultyProvider(_mock_provider(), RepeatDestroyFailsSandbox)
    )


def _exit_code_mislabeling() -> ConformanceTarget:
    return _target("mislabels-exit-code", FaultyProvider(_mock_provider(), MislabelingSandbox))


def _network_off_ignored() -> ConformanceTarget:
    # The mock validates network policies but does not enforce them.
    return _target("ignores-network-off", _mock_provider(), observer="127.0.0.1")


def _network_off_enforced() -> ConformanceTarget:
    return _target(
        "enforces-network-off", OffModeProxyProvider(_mock_provider()), observer="127.0.0.1"
    )


# -- harness --------------------------------------------------------------------------------


class TargetsPlugin:
    """Supplies targets to the conformance plugin through its ``pytest_spleef_targets`` hook."""

    def __init__(self, targets: list[ConformanceTarget]) -> None:
        self._targets = targets

    def pytest_spleef_targets(self, config: pytest.Config) -> list[ConformanceTarget]:
        return self._targets


def _restrict_to_cases(monkeypatch: pytest.MonkeyPatch, case_ids: set[str]) -> None:
    """Make the plugin load only the named cases (generated items ignore ``-k``)."""
    selected = tuple(case for case in plugin.load_cases() if case.id in case_ids)
    assert {case.id for case in selected} == case_ids
    module = types.ModuleType("negative_proof_cases")
    module.CASES = selected
    monkeypatch.setitem(sys.modules, "negative_proof_cases", module)
    monkeypatch.setattr(plugin, "CASE_MODULES", ("negative_proof_cases",))


def _run(pytester: pytest.Pytester, targets: list[ConformanceTarget]) -> list[dict[str, Any]]:
    """Run the plugin against ``targets`` and return the report entries."""
    args = [f"--spleef-provider={target.name}" for target in targets]
    pytester.runpytest(*args, "--spleef-report=conf/report", plugins=[TargetsPlugin(targets)])
    data = json.loads((pytester.path / "conf" / "report.json").read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = data["results"]
    return results


@dataclass(frozen=True)
class Fault:
    case_id: str
    broken: Callable[[], ConformanceTarget]
    control: Callable[[], ConformanceTarget]
    detail: str
    """Fragment of the failure message that names the defect."""


FAULTS = [
    Fault("C-EXEC-03", _timeout_ignoring, _plain_mock, "did not report a timeout"),
    Fault("C-ISO-01", _marker_leaking, _plain_mock, "sees the marker"),
    Fault("C-LIFE-02", _destroy_not_idempotent, _plain_mock, "already destroyed"),
    Fault("C-EXEC-01", _exit_code_mislabeling, _plain_mock, "reported exit 0"),
    Fault("C-NET-01", _network_off_ignored, _network_off_enforced, "reachable from the sandbox"),
]


@pytest.mark.parametrize("fault", FAULTS, ids=lambda fault: fault.case_id)
def test_broken_provider_fails_the_matching_case(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> None:
    _restrict_to_cases(monkeypatch, {fault.case_id})
    (entry,) = _run(pytester, [fault.broken()])
    assert entry["case_id"] == fault.case_id
    assert entry["status"] == "fail", entry
    assert fault.detail in entry["detail"], entry["detail"]


@pytest.mark.parametrize("fault", FAULTS, ids=lambda fault: fault.case_id)
def test_control_provider_passes_the_same_case(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> None:
    _restrict_to_cases(monkeypatch, {fault.case_id})
    (entry,) = _run(pytester, [fault.control()])
    assert entry["case_id"] == fault.case_id
    assert entry["status"] == "pass", entry


def test_builtin_targets_pass_their_own_case_set(pytester: pytest.Pytester) -> None:
    """The mock sandbox and the three mock model providers must not fail any case."""
    targets = build_targets()
    results = _run(pytester, targets)
    failed = [(r["target"], r["case_id"], r["detail"]) for r in results if r["status"] == "fail"]
    assert failed == []
    passed_by_target: dict[str, set[str]] = {target.name: set() for target in targets}
    for entry in results:
        if entry["status"] == "pass":
            passed_by_target[entry["target"]].add(entry["case_id"])
    covered = {fault.case_id for fault in FAULTS if fault.case_id != "C-NET-01"}
    assert covered <= passed_by_target["mock-sandbox"]
