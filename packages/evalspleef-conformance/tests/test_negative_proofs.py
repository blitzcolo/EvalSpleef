"""Negative proofs: a conformance case must be able to fail.

Each broken provider below wraps the mock sandbox and breaks exactly one behaviour. The
matching case has to FAIL against the broken provider and PASS against its control (the
unbroken mock, or a provider that enforces the policy the broken one ignores), so a red
result is caused by the defect and not by the harness. The builtin mock targets must pass
their own full case set.
"""

from __future__ import annotations

import asyncio
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
from evalspleef.conformance.cases_sandbox_roots import WORKSPACE
from evalspleef.conformance.target import ConformanceTarget, ObserverHosts
from evalspleef.spi.errors import SandboxLost, TransferError
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    ExecResult,
    NetworkPolicy,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver

READ_ONLY_ENFORCEABLE = default_capabilities().read_only_upload
"""The mock enforces read-only uploads only where the host has a user-immutable file flag."""

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

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        await self.inner.upload(local, remote, read_only=read_only)

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


class NestingUploadSandbox(DelegatingSandbox):
    """Uploads a directory aimed at the workspace mount point as a child of it."""

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if remote == WORKSPACE and await asyncio.to_thread(local.is_dir):
            remote = f"{WORKSPACE}/{local.name}"
        await self.inner.upload(local, remote, read_only=read_only)


class WorkspaceOnlySandbox(DelegatingSandbox):
    """A read-only root with only the workspace writable, declared paths ignored."""

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if remote != WORKSPACE and not remote.startswith(f"{WORKSPACE}/"):
            raise TransferError(f"cannot create {remote}: Read-only file system")
        await self.inner.upload(local, remote, read_only=read_only)


class WritableReadOnlySandbox(DelegatingSandbox):
    """Accepts ``read_only=True`` but leaves the uploaded tree writable."""

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        await self.inner.upload(local, remote)


class ReplacingUploadSandbox(DelegatingSandbox):
    """Replaces an existing directory on upload instead of merging into it."""

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if await asyncio.to_thread(local.is_dir):
            parent, _, name = remote.rpartition("/")
            await self.inner.exec(["rm", "-rf", name], cwd=parent or "/")
        await self.inner.upload(local, remote, read_only=read_only)


class PathIgnoringProvider(DelegatingProvider):
    """Accepts declared writable paths and silently drops them."""

    def __init__(self, inner: MockSandboxProvider, *, declares: bool) -> None:
        super().__init__(inner)
        self._declares = declares

    def capabilities(self) -> SandboxCapabilities:
        if self._declares:
            return self.inner.capabilities()
        return replace(self.inner.capabilities(), writable_paths=False, read_only_upload=False)

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await self.inner.create(replace(spec, writable_paths=()))


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


class OffModeHostOnlyProvider(DelegatingProvider):
    """Blocks the primary observer under ``off`` but leaves the host reachable at 127.0.0.1,
    like a host-only network whose gateway differs from the primary observer address."""

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if spec.network.mode.value == "off":
            env = {
                **spec.env,
                "http_proxy": BLACKHOLE_PROXY,
                "all_proxy": BLACKHOLE_PROXY,
                "no_proxy": "127.0.0.1",
            }
            spec = replace(spec, env=env)
        return await self.inner.create(spec)


async def _loopback_observer() -> list[str]:
    return ["127.0.0.1"]


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
    observer_hosts: ObserverHosts | None = None,
) -> ConformanceTarget:
    (builtin,) = [target for target in build_targets() if target.name == "mock-sandbox"]
    return ObservedTarget(
        name=name,
        kind="sandbox",
        provider=provider,
        fake=True,
        spec_factory=builtin.spec_factory,
        cleanup=cleanup,
        observer_hosts=observer_hosts,
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


def _upload_nests_directory() -> ConformanceTarget:
    return _target(
        "nests-uploaded-directory", FaultyProvider(_mock_provider(), NestingUploadSandbox)
    )


def _no_verify_root() -> ConformanceTarget:
    return _target("no-verify-root", FaultyProvider(_mock_provider(), WorkspaceOnlySandbox))


def _declared_paths_dropped() -> ConformanceTarget:
    return _target("drops-declared-paths", PathIgnoringProvider(_mock_provider(), declares=True))


def _read_only_left_writable() -> ConformanceTarget:
    return _target(
        "read-only-left-writable", FaultyProvider(_mock_provider(), WritableReadOnlySandbox)
    )


def _undeclared_paths_accepted() -> ConformanceTarget:
    return _target(
        "accepts-undeclared-paths", PathIgnoringProvider(_mock_provider(), declares=False)
    )


def _undeclared_paths_refused() -> ConformanceTarget:
    caps = replace(default_capabilities(), writable_paths=False, read_only_upload=False)
    provider = MockSandboxProvider("negative-proof", {}, DictSecretResolver(), capabilities=caps)
    return _target("refuses-undeclared-paths", provider)


def _upload_replaces_directory() -> ConformanceTarget:
    return _target(
        "replaces-uploaded-directory", FaultyProvider(_mock_provider(), ReplacingUploadSandbox)
    )


def _host_only_off() -> ConformanceTarget:
    # The primary observer goes through the refusing proxy, the host-only address does not.
    return _target(
        "host-only-off",
        OffModeHostOnlyProvider(_mock_provider()),
        observer="localhost",
        observer_hosts=_loopback_observer,
    )


def _off_enforced_on_every_address() -> ConformanceTarget:
    return _target(
        "off-on-every-address",
        OffModeProxyProvider(_mock_provider()),
        observer="localhost",
        observer_hosts=_loopback_observer,
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
    Fault(
        "C-NET-01", _host_only_off, _off_enforced_on_every_address, "must not be: http://127.0.0.1:"
    ),
    Fault("C-FILE-05", _upload_nests_directory, _plain_mock, "are not at /workspace/<name>"),
    Fault("C-FILE-06", _no_verify_root, _plain_mock, "Read-only file system"),
    Fault("C-PATH-01", _declared_paths_dropped, _plain_mock, "cwd does not exist"),
    Fault("C-PATH-02", _read_only_left_writable, _plain_mock, "in a read-only upload"),
    Fault(
        "C-PATH-03",
        _undeclared_paths_accepted,
        _undeclared_paths_refused,
        "must raise PolicyNotEnforceable",
    ),
    Fault("C-FILE-07", _upload_replaces_directory, _plain_mock, "must merge"),
]


def _skip_unenforceable(fault: Fault) -> None:
    if fault.case_id == "C-PATH-02" and not READ_ONLY_ENFORCEABLE:
        pytest.skip("the mock cannot enforce read-only uploads without user-immutable file flags")


def _fault_id(fault: Fault) -> str:
    return f"{fault.case_id}-{fault.broken.__name__.strip('_')}"


@pytest.mark.parametrize("fault", FAULTS, ids=_fault_id)
def test_broken_provider_fails_the_matching_case(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> None:
    _skip_unenforceable(fault)
    _restrict_to_cases(monkeypatch, {fault.case_id})
    (entry,) = _run(pytester, [fault.broken()])
    assert entry["case_id"] == fault.case_id
    assert entry["status"] == "fail", entry
    assert fault.detail in entry["detail"], entry["detail"]


@pytest.mark.parametrize("fault", FAULTS, ids=_fault_id)
def test_control_provider_passes_the_same_case(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> None:
    _skip_unenforceable(fault)
    _restrict_to_cases(monkeypatch, {fault.case_id})
    (entry,) = _run(pytester, [fault.control()])
    assert entry["case_id"] == fault.case_id
    assert entry["status"] == "pass", entry


def test_verify_root_follows_the_control_plane_override(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A carrier that only writes under /workspace passes C-FILE-06 once SPLEEF_VERIFY_ROOT
    moves the verify root there, as the control plane would run its verifier."""
    monkeypatch.setenv("SPLEEF_VERIFY_ROOT", f"{WORKSPACE}/verify")
    _restrict_to_cases(monkeypatch, {"C-FILE-06"})
    (entry,) = _run(pytester, [_no_verify_root()])
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
    # C-NET-01 needs an observer and C-PATH-03 applies only without the capabilities.
    covered = {f.case_id for f in FAULTS if f.case_id not in ("C-NET-01", "C-PATH-03")}
    if not READ_ONLY_ENFORCEABLE:
        covered.discard("C-PATH-02")
    assert covered <= passed_by_target["mock-sandbox"]
