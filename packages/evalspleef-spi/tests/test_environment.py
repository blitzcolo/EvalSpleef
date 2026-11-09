"""Execution environment value types, the default provider report and the local host collector."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from evalspleef.spi import environment as env_module
from evalspleef.spi.environment import (
    ExecutionEnvironment,
    MachineFacts,
    RuntimeFacts,
    local_host_facts,
    normalise_arch,
    os_family,
    parse_os_release,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import Sandbox, SandboxCapabilities, SandboxProvider, SandboxSpec


class _SilentProvider(SandboxProvider):
    """An adapter that implements only the mandatory surface."""

    name = "silent"

    def capabilities(self) -> SandboxCapabilities:
        raise NotImplementedError

    def isolation_profile(self) -> IsolationProfile:
        raise NotImplementedError

    async def preflight(self) -> dict[str, Any]:
        return {}

    async def create(self, spec: SandboxSpec) -> Sandbox:
        raise NotImplementedError

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        return []

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        return 0


async def test_an_adapter_without_an_override_reports_nothing() -> None:
    report = await _SilentProvider("silent-1", {}, object()).environment()  # type: ignore[arg-type]

    assert report == ExecutionEnvironment()
    assert report.reported is False
    as_dict = report.to_dict()
    assert set(as_dict) == {"host", "runtime", "guest", "extra"}
    assert all(value is None for value in as_dict["host"].values())
    assert all(value is None for value in as_dict["runtime"].values())
    assert all(value is None for value in as_dict["guest"].values())
    assert as_dict["extra"] == {}


@pytest.mark.parametrize(
    "report",
    [
        ExecutionEnvironment(host=MachineFacts(os="linux")),
        ExecutionEnvironment(runtime=RuntimeFacts(version="1")),
        ExecutionEnvironment(guest=MachineFacts(kernel="6.1")),
        ExecutionEnvironment(extra={"api_version": "1.56"}),
    ],
)
def test_any_single_fact_counts_as_reported(report: ExecutionEnvironment) -> None:
    assert report.reported is True


def test_to_dict_is_plain_json_with_sorted_string_extras() -> None:
    report = ExecutionEnvironment(
        host=MachineFacts(os="macos", cpu_count=12, memory_bytes=1 << 34),
        runtime=RuntimeFacts(name="docker", version="29.9.2"),
        extra={"z": "1", "a": "2"},
    )

    as_dict = report.to_dict()

    assert as_dict["host"]["cpu_count"] == 12
    assert as_dict["host"]["memory_bytes"] == 1 << 34
    assert as_dict["runtime"] == {"name": "docker", "version": "29.9.2"}
    assert list(as_dict["extra"]) == ["a", "z"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("aarch64", "arm64"), ("ARM64", "arm64"), ("amd64", "x86_64"), ("riscv64", "riscv64")],
)
def test_architectures_use_the_arch_spellings(raw: str, expected: str) -> None:
    assert normalise_arch(raw) == expected


def test_empty_architecture_stays_unreported() -> None:
    assert normalise_arch("") is None
    assert normalise_arch(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Darwin", "macos"), ("Linux", "linux"), ("linux", "linux"), ("Windows", "windows")],
)
def test_os_family_maps_uname_and_engine_spellings(raw: str, expected: str) -> None:
    assert os_family(raw) == expected


def test_os_release_is_parsed_without_executing_it() -> None:
    text = "# comment\nNAME=\"Debian GNU/Linux\"\nVERSION_ID=12\nPRETTY_NAME='Debian 12'\nBROKEN\n"

    assert parse_os_release(text) == {
        "NAME": "Debian GNU/Linux",
        "VERSION_ID": "12",
        "PRETTY_NAME": "Debian 12",
    }


def test_local_host_facts_describe_this_machine() -> None:
    facts = local_host_facts()

    assert facts.os in {"macos", "linux", "windows"}
    assert facts.kernel
    assert facts.arch in {"arm64", "x86_64"}
    assert facts.cpu_count is not None and facts.cpu_count >= 1
    assert facts.memory_bytes is not None and facts.memory_bytes > 1 << 28


def test_local_host_facts_on_linux_read_os_release_cpuinfo_and_the_board_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {
        "/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n',
        "/proc/cpuinfo": "processor\t: 0\nmodel name\t: AMD EPYC 9654 96-Core Processor\n",
        "/sys/class/dmi/id/product_name": "PowerEdge R7625\n",
    }
    monkeypatch.setattr(env_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(env_module.platform, "release", lambda: "6.8.0-45-generic")
    monkeypatch.setattr(env_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(env_module, "_read", files.get)

    facts = local_host_facts()

    assert facts.os == "linux"
    assert facts.os_version == "Ubuntu 24.04.1 LTS"
    assert facts.kernel == "6.8.0-45-generic"
    assert facts.cpu_model == "AMD EPYC 9654 96-Core Processor"
    assert facts.machine_model == "PowerEdge R7625"


def test_unreadable_host_facts_stay_none_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_sysctl(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="sysctl", timeout=5)

    def no_file(self: Path, *args: Any, **kwargs: Any) -> str:
        raise PermissionError(str(self))

    monkeypatch.setattr(env_module.subprocess, "run", no_sysctl)
    monkeypatch.setattr(env_module.Path, "read_text", no_file)
    monkeypatch.setattr(env_module.platform, "mac_ver", lambda: ("", ("", "", ""), ""))

    for system in ("Darwin", "Linux"):
        monkeypatch.setattr(env_module.platform, "system", lambda value=system: value)
        facts = local_host_facts()
        assert facts.cpu_model is None
        assert facts.machine_model is None
        assert facts.os_version is None
