"""Guest measurement through the Sandbox SPI and provider environment records."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalspleef.control.environment import (
    ProviderEnvironments,
    measure_guest,
    parse_guest_report,
    provider_environment_record,
    sandbox_record,
    with_sandbox,
)
from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.environment import ExecutionEnvironment, os_family
from evalspleef.spi.errors import SandboxLost
from evalspleef.spi.sandbox import (
    ExecResult,
    NetworkPolicy,
    Resources,
    Sandbox,
    SandboxProvider,
    SandboxSpec,
    TemplateRef,
)
from evalspleef.testing.mock_sandbox import MOCK_ENVIRONMENT, MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

LINUX_REPORT = """\
@uname
Linux 7.0.14-linuxkit aarch64
@ncpu
12
@meminfo
MemTotal:        8124196 kB
@os-release
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
"""

MACOS_REPORT = """\
@uname
Darwin 25.1.0 arm64
@ncpu
8
@memsize
17179869184
@product
26.1
"""


def _spec() -> SandboxSpec:
    return SandboxSpec(
        trial_id="t1",
        lease_id="l1",
        role="agent",
        os=OSFamily.LINUX,
        arch=Arch.X86_64,
        template=TemplateRef(kind="oci", ref="img:1", digest="sha256:" + "a" * 64),
        resources=Resources(cpus=1, memory_mb=512, disk_mb=512),
        network=NetworkPolicy(mode=NetMode.OFF),
    )


class _ScriptedSandbox(Sandbox):
    """A sandbox whose exec answers with a fixed result or raises."""

    def __init__(self, result: ExecResult | BaseException) -> None:
        self.id = "sb-scripted"
        self.spec = _spec()
        self._result = result

    async def exec(self, cmd: str | list[str], **kwargs: Any) -> ExecResult:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def upload(self, local: Path, remote: str) -> None:
        raise NotImplementedError

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        raise NotImplementedError

    async def destroy(self) -> None:
        return None


def _result(stdout: str, exit_code: int = 0, *, timed_out: bool = False) -> ExecResult:
    return ExecResult(exit_code, stdout.encode(), b"boom", timed_out, 0.01)


def test_a_linux_report_yields_every_guest_fact() -> None:
    facts = parse_guest_report(LINUX_REPORT)

    assert facts.os == "linux"
    assert facts.kernel == "7.0.14-linuxkit"
    assert facts.arch == "arm64"
    assert facts.cpu_count == 12
    assert facts.memory_bytes == 8124196 * 1024
    assert facts.os_version == "Debian GNU/Linux 12 (bookworm)"


def test_a_macos_report_yields_every_guest_fact() -> None:
    facts = parse_guest_report(MACOS_REPORT)

    assert (facts.os, facts.kernel, facts.arch) == ("macos", "25.1.0", "arm64")
    assert (facts.cpu_count, facts.memory_bytes, facts.os_version) == (8, 17179869184, "26.1")


def test_missing_sections_leave_only_their_facts_empty() -> None:
    facts = parse_guest_report("@uname\nLinux 6.1.0 x86_64\n@ncpu\n@meminfo\n")

    assert facts.kernel == "6.1.0"
    assert facts.cpu_count is None
    assert facts.memory_bytes is None
    assert facts.os_version is None


async def test_a_mock_sandbox_is_measured_on_the_machine_it_really_runs_on() -> None:
    provider = MockSandboxProvider("mock", {}, DictSecretResolver())
    sandbox = await provider.create(_spec())
    try:
        record = await measure_guest(sandbox, OSFamily.LINUX)
    finally:
        await sandbox.destroy()

    uname = os.uname()
    assert record["status"] == "measured"
    assert record["guest"]["kernel"] == uname.release
    assert record["guest"]["os"] == os_family(uname.sysname)
    assert record["guest"]["cpu_count"] == os.cpu_count()


async def test_an_os_family_without_a_command_set_is_unmeasured_with_the_reason() -> None:
    record = await measure_guest(_ScriptedSandbox(_result(LINUX_REPORT)), OSFamily.WINDOWS)

    assert record["status"] == "unmeasured"
    assert "windows" in record["reason"]


async def test_a_failing_exec_is_unmeasured_not_raised() -> None:
    record = await measure_guest(_ScriptedSandbox(SandboxLost("gone")), OSFamily.LINUX)

    assert record["status"] == "unmeasured"
    assert "SandboxLost" in record["reason"]


async def test_a_non_zero_exit_or_timeout_is_unmeasured() -> None:
    failed = await measure_guest(_ScriptedSandbox(_result("", 127)), OSFamily.LINUX)
    timed_out = await measure_guest(
        _ScriptedSandbox(_result("", 124, timed_out=True)), OSFamily.LINUX
    )

    assert failed["status"] == timed_out["status"] == "unmeasured"
    assert "127" in failed["reason"] and "timed out" in timed_out["reason"]


async def test_output_without_a_kernel_release_is_unmeasured() -> None:
    record = await measure_guest(_ScriptedSandbox(_result("@uname\n\n")), OSFamily.LINUX)

    assert record["status"] == "unmeasured"


async def test_the_mock_provider_reports_its_fixed_environment() -> None:
    record = await provider_environment_record(MockSandboxProvider("m", {}, DictSecretResolver()))

    assert record["status"] == "reported"
    assert record["runtime"] == {"name": "mock-sandbox", "version": "1.0.0"}
    assert record["host"]["machine_model"] == MOCK_ENVIRONMENT.host.machine_model


class _Unreported(MockSandboxProvider):
    async def environment(self) -> ExecutionEnvironment:
        return ExecutionEnvironment()


class _Broken(MockSandboxProvider):
    async def environment(self) -> ExecutionEnvironment:
        raise SandboxLost("engine unreachable")


async def test_an_empty_or_failing_report_is_recorded_as_unreported_with_the_reason() -> None:
    empty = await provider_environment_record(_Unreported("u", {}, DictSecretResolver()))
    broken = await provider_environment_record(_Broken("b", {}, DictSecretResolver()))

    assert empty["status"] == broken["status"] == "unreported"
    assert "does not report" in empty["reason"]
    assert "engine unreachable" in broken["reason"]
    assert broken["runtime"] == {"name": None, "version": None}


class _CountingProvider(MockSandboxProvider):
    def __init__(self) -> None:
        super().__init__("counted", {}, DictSecretResolver())
        self.calls = 0

    async def environment(self) -> ExecutionEnvironment:
        self.calls += 1
        await asyncio.sleep(0)
        return MOCK_ENVIRONMENT


class _Directory:
    def __init__(self, providers: Mapping[str, SandboxProvider]) -> None:
        self._providers = providers

    def sandbox_instance(self, name: str) -> SandboxProvider:
        return self._providers[name]


async def test_each_instance_is_asked_once_per_run_and_callers_get_private_copies() -> None:
    provider = _CountingProvider()
    environments = ProviderEnvironments(_Directory({"counted": provider}))

    first, second, third = await asyncio.gather(*(environments.get("counted") for _ in range(3)))
    first["runtime"]["name"] = "tampered"

    assert provider.calls == 1
    assert second["runtime"]["name"] == third["runtime"]["name"] == "mock-sandbox"
    assert (await environments.get("counted"))["runtime"]["name"] == "mock-sandbox"


def test_a_late_sandbox_joins_a_copy_of_the_versions() -> None:
    versions = {"task_version": "1", "execution_environment": {"sandboxes": {"agent": {}}}}

    merged = with_sandbox(versions, "verifier", sandbox_record("docker-b", None))

    assert merged["execution_environment"]["sandboxes"]["verifier"] == {
        "provider_instance": "docker-b",
        "status": "unmeasured",
        "reason": "the lease carries no guest record",
    }
    assert "verifier" not in versions["execution_environment"]["sandboxes"]
