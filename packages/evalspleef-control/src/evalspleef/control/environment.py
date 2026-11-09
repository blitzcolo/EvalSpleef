"""Execution environment records: provider facts and measured guest facts (AGENTS.md rule 8).

Two sources, kept apart because they answer different questions:

- the *provider* environment (``SandboxProvider.environment()``): host machine, sandbox
  runtime and the guest as the runtime reports it. Collected once per provider instance
  per batch run (:class:`ProviderEnvironments`) and shared by the freeze record and
  every trial of the run.
- the *measured* guest (:func:`measure_guest`): what a freshly created sandbox itself
  reports through the Sandbox SPI, run by the lease manager right after ``create``.
  It is what the trial actually ran on, so it is measured, never assumed from the
  template or the provider.

Neither is ever allowed to fail a lease or a trial: a provider that cannot be asked is
recorded as ``unreported`` and a guest that cannot be measured as ``unmeasured``, each
with the reason. Records are plain JSON so they travel in event payloads unchanged.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from typing import Any, Protocol

from evalspleef.spi.caps import OSFamily
from evalspleef.spi.environment import (
    ExecutionEnvironment,
    MachineFacts,
    normalise_arch,
    os_family,
    parse_os_release,
)
from evalspleef.spi.sandbox import Sandbox, SandboxProvider

__all__ = [
    "GUEST_MEASURED",
    "GUEST_UNMEASURED",
    "MEASURE_TIMEOUT_S",
    "PROVIDER_REPORTED",
    "PROVIDER_UNREPORTED",
    "ProviderEnvironments",
    "measure_guest",
    "parse_guest_report",
    "provider_environment_record",
    "sandbox_record",
    "unmeasured",
    "with_sandbox",
]

PROVIDER_REPORTED = "reported"
PROVIDER_UNREPORTED = "unreported"
GUEST_MEASURED = "measured"
GUEST_UNMEASURED = "unmeasured"

#: Budget of the read-only guest measurement; it runs once per sandbox before any trial work.
MEASURE_TIMEOUT_S = 30.0

# Read-only, portable command sets, one per guest OS family. Each section starts with an
# ``@name`` marker line so a missing tool only blanks its own section; ``exit 0`` keeps a
# missing optional file from turning the whole measurement into a failure.
_LINUX_SCRIPT = (
    "echo @uname; uname -srm; "
    "echo @ncpu; getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null; "
    "echo @meminfo; grep -m1 '^MemTotal:' /proc/meminfo 2>/dev/null; "
    "echo @os-release; head -n 40 /etc/os-release 2>/dev/null "
    "|| head -n 40 /usr/lib/os-release 2>/dev/null; "
    "exit 0"
)
_MACOS_SCRIPT = (
    "echo @uname; uname -srm; "
    "echo @ncpu; sysctl -n hw.logicalcpu 2>/dev/null; "
    "echo @memsize; sysctl -n hw.memsize 2>/dev/null; "
    "echo @product; sw_vers -productVersion 2>/dev/null; "
    "exit 0"
)
_SCRIPTS: Mapping[str, str] = {OSFamily.LINUX: _LINUX_SCRIPT, OSFamily.MACOS: _MACOS_SCRIPT}


class _SandboxProviders(Protocol):
    def sandbox_instance(self, name: str) -> SandboxProvider: ...


def unmeasured(reason: str) -> dict[str, Any]:
    """The record of a guest that could not be measured."""
    return {"status": GUEST_UNMEASURED, "reason": reason}


def _sections(stdout: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("@"):
            current = sections.setdefault(line[1:], [])
        elif current is not None and line:
            current.append(line)
    return sections


def _first_int(lines: list[str] | None) -> int | None:
    value = lines[0] if lines else ""
    return int(value) if value.isdigit() else None


def parse_guest_report(stdout: str) -> MachineFacts:
    """Guest facts from the output of a measurement command set; absent facts stay ``None``."""
    sections = _sections(stdout)
    uname = (sections.get("uname") or [""])[0].split()
    sysname, kernel, machine = uname if len(uname) == 3 else (None, None, None)
    family = os_family(sysname)
    memory: int | None = None
    os_version: str | None = None
    meminfo = (sections.get("meminfo") or [""])[0].split()
    if len(meminfo) >= 2 and meminfo[1].isdigit():
        # /proc/meminfo reports kibibytes whatever the unit column says.
        memory = int(meminfo[1]) * 1024
    elif sections.get("memsize"):
        memory = _first_int(sections.get("memsize"))
    if sections.get("os-release"):
        release = parse_os_release("\n".join(sections["os-release"]))
        os_version = release.get("PRETTY_NAME") or release.get("NAME") or None
    elif sections.get("product"):
        os_version = sections["product"][0]
    return MachineFacts(
        os=family,
        os_version=os_version,
        kernel=kernel,
        arch=normalise_arch(machine),
        cpu_count=_first_int(sections.get("ncpu")),
        memory_bytes=memory,
    )


async def measure_guest(sandbox: Sandbox, family: str) -> dict[str, Any]:
    """Measure a fresh sandbox through the Sandbox SPI; never raises.

    Returns ``{"status": "measured", "guest": {...}}`` or
    ``{"status": "unmeasured", "reason": ...}``.
    """
    script = _SCRIPTS.get(family)
    if script is None:
        return unmeasured(f"no guest measurement command set for os family {family!s}")
    try:
        result = await asyncio.wait_for(
            sandbox.exec(script, timeout_s=MEASURE_TIMEOUT_S), MEASURE_TIMEOUT_S + 10
        )
    except Exception as exc:  # recorded in sandbox.provisioned and trial.versions, not swallowed
        return unmeasured(f"measurement exec failed: {type(exc).__name__}: {exc}")
    if result.timed_out:
        return unmeasured(f"measurement timed out after {MEASURE_TIMEOUT_S:g}s")
    if result.exit_code != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()[:200]
        return unmeasured(f"measurement exited {result.exit_code}: {stderr}")
    facts = parse_guest_report(result.stdout.decode("utf-8", errors="replace"))
    if facts.kernel is None:
        return unmeasured("the sandbox reported no kernel release (uname -srm)")
    return {"status": GUEST_MEASURED, "guest": facts.to_dict()}


async def provider_environment_record(provider: SandboxProvider) -> dict[str, Any]:
    """The provider's ``environment()`` as a record; never raises.

    The record always has the ``host``/``runtime``/``guest``/``extra`` keys, so readers
    need no special case; ``status`` says whether anything was reported and ``reason``
    why not.
    """
    try:
        environment = await provider.environment()
    except Exception as exc:  # recorded in the freeze record and every trial's versions
        return {
            "status": PROVIDER_UNREPORTED,
            "reason": f"{type(exc).__name__}: {exc}",
            **ExecutionEnvironment().to_dict(),
        }
    record: dict[str, Any] = {
        "status": PROVIDER_REPORTED if environment.reported else PROVIDER_UNREPORTED,
        **environment.to_dict(),
    }
    if not environment.reported:
        record["reason"] = f"the {provider.name} adapter does not report its environment"
    return record


class ProviderEnvironments:
    """Provider environment records, collected once per provider instance per batch run.

    One instance lives as long as a batch run (the CLI builds it once per command), so
    the freeze record and every trial of the run carry the same snapshot. Concurrent
    trials on one instance share a single in-flight collection.
    """

    def __init__(self, providers: _SandboxProviders) -> None:
        self._providers = providers
        self._records: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def get(self, instance: str) -> dict[str, Any]:
        """A private copy of the record of ``instance``."""
        future = self._records.get(instance)
        if future is None:
            future = asyncio.ensure_future(
                provider_environment_record(self._providers.sandbox_instance(instance))
            )
            self._records[instance] = future
        return copy.deepcopy(await future)


def sandbox_record(instance: str, guest: Mapping[str, Any] | None) -> dict[str, Any]:
    """The per-sandbox entry of ``execution_environment.sandboxes``."""
    measured = dict(guest) if guest is not None else unmeasured("the lease carries no guest record")
    return {"provider_instance": instance, **measured}


def with_sandbox(
    versions: Mapping[str, Any], role: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    """A copy of ``versions`` whose execution environment also lists the ``role`` sandbox.

    Used for the verifier, which is created after ``trial.started`` was recorded; its
    measurement is in its own ``sandbox.provisioned`` event and joins the row here.
    """
    merged = copy.deepcopy(dict(versions))
    environment = merged.get("execution_environment")
    if not isinstance(environment, dict):
        return merged
    environment.setdefault("sandboxes", {})[role] = dict(record)
    return merged
