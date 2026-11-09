"""Execution environment facts: where a sandbox actually runs (AGENTS.md rule 8).

A provider reports the environment it runs sandboxes in through
``SandboxProvider.environment()``: the host machine, the sandbox runtime or engine,
and the guest (the container VM or virtual machine) as the runtime sees it. The
control plane records the report with every batch and trial so a result can be
reproduced on the same stack.

Every field is optional and ``None`` means "not reported", never a guess. Only stable
facts belong here: no memory in use, uptime, load, container counts or clock
readings. Two reports of an unchanged machine are therefore equal, and anything
hashed from them (the batch version matrix) stays deterministic.

:func:`local_host_facts` collects the facts of the machine the caller runs on, for
adapters whose carrier is local. It only reads (``platform``, ``os``, ``sysctl`` on
macOS, ``/proc`` and ``/sys`` on Linux) and leaves a fact it cannot read as ``None``.
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ExecutionEnvironment",
    "MachineFacts",
    "RuntimeFacts",
    "local_host_facts",
    "normalise_arch",
    "os_family",
    "parse_os_release",
]


@dataclass(frozen=True)
class MachineFacts:
    """One machine, physical or virtual, as far as it was reported."""

    os: str | None = None
    """OS family: ``linux``, ``macos``, ``windows`` (the ``OSFamily`` spellings)."""
    os_version: str | None = None
    """Product or distribution release, for example ``27.2`` or ``Debian GNU/Linux 12``."""
    kernel: str | None = None
    """Kernel release, for example ``27.2.0`` or ``6.10.14-linuxkit``."""
    arch: str | None = None
    """CPU architecture, normalised to the ``Arch`` spellings where known."""
    cpu_model: str | None = None
    cpu_count: int | None = None
    """Logical CPUs the machine (or the guest) sees."""
    memory_bytes: int | None = None
    """Physical memory, or the memory assigned to a virtual machine."""
    machine_model: str | None = None
    """Hardware model identifier, for example ``Mac18,5``."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeFacts:
    """The sandbox runtime or engine, for example ``docker`` ``29.9.2``."""

    name: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionEnvironment:
    """What a provider instance runs on. The default instance reports nothing."""

    host: MachineFacts = field(default_factory=MachineFacts)
    runtime: RuntimeFacts = field(default_factory=RuntimeFacts)
    guest: MachineFacts = field(default_factory=MachineFacts)
    """The machine sandboxes run in as the runtime sees it (a container VM, a VM)."""
    extra: Mapping[str, str] = field(default_factory=dict)
    """Carrier facts without a field of their own (API version, component versions)."""

    @property
    def reported(self) -> bool:
        """Whether the provider reported any fact at all."""
        return (
            self.host != MachineFacts()
            or self.runtime != RuntimeFacts()
            or self.guest != MachineFacts()
            or bool(self.extra)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.to_dict(),
            "runtime": self.runtime.to_dict(),
            "guest": self.guest.to_dict(),
            "extra": {str(key): str(value) for key, value in sorted(self.extra.items())},
        }


_ARCH_ALIASES = {"aarch64": "arm64", "arm64": "arm64", "amd64": "x86_64", "x86_64": "x86_64"}
_OS_FAMILIES = {"darwin": "macos", "linux": "linux", "windows": "windows"}


def normalise_arch(raw: str | None) -> str | None:
    """``aarch64`` -> ``arm64``, ``amd64`` -> ``x86_64``; unknown values pass through."""
    if not raw:
        return None
    value = raw.strip()
    return _ARCH_ALIASES.get(value.lower(), value) or None


def os_family(sysname: str | None) -> str | None:
    """The OS family of a ``uname -s`` / ``platform.system()`` / Docker ``OSType`` value."""
    if not sysname:
        return None
    value = sysname.strip()
    return _OS_FAMILIES.get(value.lower(), value.lower()) or None


def parse_os_release(text: str) -> dict[str, str]:
    """The ``KEY=value`` pairs of an ``os-release`` file, quotes removed (never executed)."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs[key] = value
    return pairs


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _sysctl(key: str) -> str | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", key],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and value.strip().isdigit() else None


def _sysconf_memory() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    return pages * page_size if pages > 0 and page_size > 0 else None


def _linux_cpu_model() -> str | None:
    text = _read("/proc/cpuinfo") or ""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("model name", "Model", "Hardware") and value.strip():
            return value.strip()
    return None


def _linux_machine_model() -> str | None:
    for path in ("/sys/firmware/devicetree/base/model", "/sys/class/dmi/id/product_name"):
        text = _read(path)
        if text and text.strip("\x00\n "):
            return text.strip("\x00\n ")
    return None


def local_host_facts() -> MachineFacts:
    """Facts of the machine this process runs on; unreadable facts stay ``None``.

    Blocking (it may run ``sysctl``): async callers use ``asyncio.to_thread``.
    """
    system = platform.system()
    family = os_family(system)
    os_version: str | None = None
    cpu_model: str | None = None
    machine_model: str | None = None
    memory: int | None = None
    if family == "macos":
        os_version = platform.mac_ver()[0] or None
        cpu_model = _sysctl("machdep.cpu.brand_string")
        machine_model = _sysctl("hw.model")
        memory = _int(_sysctl("hw.memsize"))
    elif family == "linux":
        os_version = parse_os_release(_read("/etc/os-release") or "").get("PRETTY_NAME") or None
        cpu_model = _linux_cpu_model()
        machine_model = _linux_machine_model()
    elif family == "windows":
        os_version = platform.version() or None
        cpu_model = platform.processor() or None
    return MachineFacts(
        os=family,
        os_version=os_version,
        kernel=platform.release() or None,
        arch=normalise_arch(platform.machine()),
        cpu_model=cpu_model,
        cpu_count=os.cpu_count(),
        memory_bytes=memory if memory is not None else _sysconf_memory(),
        machine_model=machine_model,
    )
