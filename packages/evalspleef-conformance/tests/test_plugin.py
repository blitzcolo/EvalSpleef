"""End-to-end tests of the pytest plugin using synthetic case modules and targets."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

import evalspleef.conformance.plugin as plugin
from evalspleef.conformance.case import Case
from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver

TARGET_CONFTEST = """
import test_target_defs
from evalspleef.conformance.target import ConformanceTarget


def pytest_spleef_targets(config):
    return test_target_defs.TARGETS
"""

TARGET_DEFS = """
from dataclasses import replace

from evalspleef.spi.caps import NetMode
from evalspleef.spi.sandbox import NetworkPolicy, Resources, SandboxSpec, TemplateRef
from evalspleef.testing.mock_sandbox import MockSandboxProvider, default_capabilities
from evalspleef.testing.secrets import DictSecretResolver

from evalspleef.conformance.target import ConformanceTarget


def spec_factory(trial_id, *, network, max_lifetime_s):
    return SandboxSpec(
        trial_id=trial_id,
        lease_id=f"lease-{trial_id}",
        role="agent",
        os="linux",
        arch="arm64",
        template=TemplateRef(kind="oci", ref="mock", digest="sha256:" + "0" * 64),
        resources=Resources(cpus=1, memory_mb=256, disk_mb=512),
        network=NetworkPolicy(mode=network),
        max_lifetime_s=max_lifetime_s,
    )


def _target(name, capabilities):
    return ConformanceTarget(
        name=name,
        kind="sandbox",
        provider=MockSandboxProvider(name, {}, DictSecretResolver(), capabilities=capabilities),
        spec_factory=spec_factory,
        fake=False,
    )


TARGETS = [
    _target("no-streaming", replace(default_capabilities(), streaming=False)),
    _target("streaming", default_capabilities()),
]
"""


def _sandbox_target(name: str, streaming: bool) -> ConformanceTarget:
    capabilities = replace(default_capabilities(), streaming=streaming)
    provider = MockSandboxProvider(name, {}, DictSecretResolver(), capabilities=capabilities)

    def spec_factory(trial_id: str, *, network: NetMode, max_lifetime_s: int) -> SandboxSpec:
        return SandboxSpec(
            trial_id=trial_id,
            lease_id=f"lease-{trial_id}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.ARM64,
            template=TemplateRef(kind="oci", ref="mock", digest="sha256:" + "0" * 64),
            resources=Resources(cpus=1, memory_mb=256, disk_mb=512),
            network=NetworkPolicy(mode=network),
            max_lifetime_s=max_lifetime_s,
        )

    return ConformanceTarget(
        name=name,
        kind="sandbox",
        provider=provider,
        spec_factory=spec_factory,
        fake=False,
    )


def _install_cases(monkeypatch: pytest.MonkeyPatch, cases: tuple[Case, ...]) -> None:
    module = types.ModuleType("synthetic_conf_cases")
    module.CASES = cases
    monkeypatch.setitem(sys.modules, "synthetic_conf_cases", module)
    monkeypatch.setattr(plugin, "CASE_MODULES", ("synthetic_conf_cases",))


def _write_targets(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(TARGET_CONFTEST)
    pytester.makefile(".py", test_target_defs=TARGET_DEFS)


def _streaming_case() -> Case:
    def applies(target: ConformanceTarget) -> str | None:
        caps = target.provider.capabilities()  # type: ignore[union-attr]
        if not caps.streaming:
            return "streaming not declared"
        return None

    async def run(target: ConformanceTarget) -> None:
        injector = target.injectors.get("lose_sandbox")
        assert injector is not None, (
            "streaming declared but target provides no 'lose_sandbox' injector"
        )

    return Case(
        id="C-TEST-STREAM",
        kind="sandbox",
        title="streaming plus fault injection",
        applies=applies,
        run=run,
    )


def _passing_case() -> Case:
    async def run(target: ConformanceTarget) -> None:
        async with target.open_sandbox() as sandbox:
            result = await sandbox.exec(["echo", "hello"])
            assert result.exit_code == 0
            assert result.stdout.strip() == b"hello"

    return Case(
        id="C-TEST-PASS",
        kind="sandbox",
        title="create, exec and destroy",
        applies=lambda target: None,
        run=run,
    )


def _model_case() -> Case:
    async def run(target: ConformanceTarget) -> None:
        return None

    return Case(
        id="M-TEST-1",
        kind="model",
        title="model placeholder",
        applies=lambda target: None,
        run=run,
    )


def test_undeclared_capability_is_not_applicable(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case needing a capability the target does not declare reports not-applicable."""
    _write_targets(pytester)
    _install_cases(monkeypatch, (_streaming_case(),))
    result = pytester.runpytest("--spleef-provider=no-streaming", "--spleef-report=conf/report")
    result.assert_outcomes(skipped=1)
    data = json.loads((pytester.path / "conf" / "report.json").read_text(encoding="utf-8"))
    (entry,) = data["results"]
    assert entry["status"] == "not-applicable"
    assert "streaming" in entry["detail"]
    assert entry["fake"] is False


def test_declared_capability_missing_injector_is_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared capability whose injector is missing fails with a clear message."""
    _write_targets(pytester)
    _install_cases(monkeypatch, (_streaming_case(),))
    result = pytester.runpytest("--spleef-provider=streaming", "--spleef-report=conf/report")
    result.assert_outcomes(failed=1)
    data = json.loads((pytester.path / "conf" / "report.json").read_text(encoding="utf-8"))
    (entry,) = data["results"]
    assert entry["status"] == "fail"
    assert "lose_sandbox" in entry["detail"]


def test_passing_case_is_recorded_and_kinds_are_filtered(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing sandbox case is reported pass; model cases never run on sandbox targets."""
    _write_targets(pytester)
    _install_cases(monkeypatch, (_passing_case(), _model_case()))
    result = pytester.runpytest("--spleef-provider=streaming", "--spleef-report=conf/report")
    result.assert_outcomes(passed=1)
    data = json.loads((pytester.path / "conf" / "report.json").read_text(encoding="utf-8"))
    assert [entry["status"] for entry in data["results"]] == ["pass"]
    assert data["results"][0]["case_id"] == "C-TEST-PASS"
    (summary,) = data["targets"]
    assert summary["passed"] == 1
    assert summary["total"] == 1


def test_unknown_provider_name_is_an_error(pytester: pytest.Pytester) -> None:
    """Selecting a target name nobody provides fails loudly instead of passing vacuously."""
    _write_targets(pytester)
    result = pytester.runpytest("--spleef-provider=does-not-exist")
    result.stderr.fnmatch_lines(["*unknown --spleef-provider*"])


def test_fully_passing_run_exits_zero(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated items count as collected, so a passing run is a success, not exit code 5."""
    _write_targets(pytester)
    _install_cases(monkeypatch, (_passing_case(),))
    result = pytester.runpytest("--spleef-provider=streaming", "--spleef-report=conf/report")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)


class _BrokenEntryPoint:
    name = "broken-adapter"

    def load(self) -> Callable[[], list[ConformanceTarget]]:
        def build() -> list[ConformanceTarget]:
            raise RuntimeError("certificate generation failed")

        return build


def test_failing_adapter_does_not_block_a_run_that_selects_another_target(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken adapter entry point is a warning, not an INTERNALERROR for unrelated runs."""
    _write_targets(pytester)
    _install_cases(monkeypatch, (_passing_case(),))
    monkeypatch.setattr(plugin, "entry_points", lambda *, group: (_BrokenEntryPoint(),))
    result = pytester.runpytest("--spleef-provider=streaming", "--spleef-report=conf/report")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*broken-adapter*RuntimeError: certificate generation failed*"])


def test_unknown_provider_error_names_adapters_that_failed_to_load(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A requested target that is missing because its adapter crashed says so."""
    _write_targets(pytester)
    monkeypatch.setattr(plugin, "entry_points", lambda *, group: (_BrokenEntryPoint(),))
    result = pytester.runpytest("--spleef-provider=broken-adapter-target")
    result.stderr.fnmatch_lines(
        ["*unknown --spleef-provider*broken-adapter*RuntimeError: certificate generation failed*"]
    )


def test_no_flag_means_no_generated_items(pytester: pytest.Pytester) -> None:
    """Without --spleef-provider the plugin leaves collection untouched."""
    pytester.makepyfile("def test_plain(): pass")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "conf").exists()


def test_open_sandbox_destroy_on_error() -> None:
    """open_sandbox destroys the sandbox even when the body raises."""
    target = _sandbox_target("destroy-check", streaming=True)
    opened: list[Any] = []

    async def body() -> None:
        async with target.open_sandbox() as sandbox:
            opened.append(sandbox)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(body())
    (sandbox,) = opened
    assert sandbox.destroyed
