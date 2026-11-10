"""Sandbox environment conformance cases (TECH-DESIGN section 15.1).

Covers the network (C-NET-*), isolation (C-ISO-*), concurrency/fault (C-CONC-01,
C-FAIL-*) and optional (C-SNAP-*, C-GUI-01, C-PORT-01) rows of the sandbox case
list. Lifecycle, execution and file cases live in ``cases_sandbox_basic``.

Networks cases that need a real observer start a small local HTTP canary server and
ask the target where that host is visible from inside its sandbox. The target
supplies it through an optional ``reachable_from_sandbox`` attribute: a bare host
name or address (no port, no scheme) reachable from inside a sandbox, for example
``host.docker.internal``. Targets without it report the observer-based cases as
not-applicable with that reason; the case never guesses.

C-NET-01 does not stop at that one address: a carrier can route ``off`` to a
host-only network whose gateway differs from the primary observer address, so the
case also probes every address from the target's ``observer_hosts`` hook and the
test machine's primary LAN address, and all of them must be unreachable.

Isolation cases run POSIX/Linux probe commands; for other OS families they report
not-applicable because the target supplies no probe script.
"""

from __future__ import annotations

import asyncio
import math
import shlex
import socket
import ssl
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from evalspleef.conformance.case import Case
from evalspleef.spi.caps import NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    NetworkPolicy,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)

if TYPE_CHECKING:
    from evalspleef.conformance.target import ConformanceTarget

_METADATA_URL = "http://169.254.169.254/"
_PROBE_TIMEOUT_S = 3.0
_CANARY_BODY = b"evalspleef-conformance-network-canary\n"


# ---------------------------------------------------------------------------
# target helpers
# ---------------------------------------------------------------------------


def _sandbox_gate(target: ConformanceTarget) -> str | None:
    """Common applicability gate: a sandbox target with a spec factory and provider."""
    if target.kind != "sandbox":
        return f"target kind is {target.kind!r}, not 'sandbox'"
    if not isinstance(target.provider, SandboxProvider):
        return "target provider does not implement SandboxProvider"
    if target.spec_factory is None:
        return "sandbox target has no spec_factory"
    return None


def _provider(target: ConformanceTarget) -> SandboxProvider:
    provider = target.provider
    if not isinstance(provider, SandboxProvider):
        raise AssertionError(f"target {target.name}: provider is not a SandboxProvider")
    return provider


def _capabilities(target: ConformanceTarget) -> SandboxCapabilities:
    return _provider(target).capabilities()


def _profile(target: ConformanceTarget) -> IsolationProfile:
    return _provider(target).isolation_profile()


def _observer(target: ConformanceTarget) -> str | None:
    """The host name/address at which the sandbox sees this process (optional attribute)."""
    value = getattr(target, "reachable_from_sandbox", None)
    if isinstance(value, str) and value:
        return value
    return None


def _host_lan_address() -> str | None:
    """The test machine's primary IPv4 address, or None when it has no route off loopback.

    Connecting a UDP socket only selects the outbound interface; no packet is sent.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed to a real host
        except OSError:
            return None  # no default route: the machine has no LAN address to probe
        address = str(probe.getsockname()[0])
    return None if address.startswith("127.") or address == "0.0.0.0" else address


async def _all_observer_hosts(target: ConformanceTarget) -> list[str]:
    """Every address through which the host observer may be reachable, primary first."""
    primary = _observer(target)
    assert primary is not None
    hosts = [primary]
    if target.observer_hosts is not None:
        hosts.extend(await target.observer_hosts())
    lan = _host_lan_address()
    if lan is not None:
        hosts.append(lan)
    return list(dict.fromkeys(host for host in hosts if host))


def _injector(target: ConformanceTarget, name: str) -> Callable[..., Awaitable[None]] | None:
    return target.injectors.get(name)


def _target_os(target: ConformanceTarget) -> OSFamily:
    factory = target.spec_factory
    if factory is None:
        raise AssertionError(f"target {target.name}: sandbox target has no spec_factory")
    spec = factory("conf-probe", network=NetMode.OFF, max_lifetime_s=600)
    return spec.os


def _posix_gate(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    os_family = _target_os(target)
    if os_family not in (OSFamily.LINUX, OSFamily.MACOS):
        return f"OS family {os_family.value!r} has no POSIX probe and the target supplies none"
    return None


def _linux_gate(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    os_family = _target_os(target)
    if os_family is not OSFamily.LINUX:
        return f"OS family {os_family.value!r} has no Linux probe and the target supplies none"
    return None


def _build_spec(
    target: ConformanceTarget,
    trial_id: str,
    *,
    network: NetMode = NetMode.OFF,
    allow_hosts: tuple[str, ...] = (),
    allow_cidrs: tuple[str, ...] = (),
    max_lifetime_s: int = 600,
) -> SandboxSpec:
    factory = target.spec_factory
    if factory is None:
        raise AssertionError(f"target {target.name}: sandbox target has no spec_factory")
    spec = factory(trial_id, network=network, max_lifetime_s=max_lifetime_s)
    if allow_hosts or allow_cidrs:
        spec = replace(
            spec,
            network=NetworkPolicy(mode=network, allow_hosts=allow_hosts, allow_cidrs=allow_cidrs),
        )
    return spec


@asynccontextmanager
async def _open_spec(target: ConformanceTarget, spec: SandboxSpec) -> AsyncIterator[Sandbox]:
    sandbox = await _provider(target).create(spec)
    try:
        yield sandbox
    finally:
        await sandbox.destroy()


# ---------------------------------------------------------------------------
# network probes
# ---------------------------------------------------------------------------


class _CanaryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(_CANARY_BODY)))
        self.end_headers()
        self.wfile.write(_CANARY_BODY)

    def log_message(self, format: str, *args: object) -> None:
        return


class _CanaryServer:
    """A loopback HTTP canary the sandbox either can or cannot reach."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), _CanaryHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, host: str) -> str:
        return f"http://{host}:{self.port}/"

    def __enter__(self) -> _CanaryServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def _http_probe_command(url: str, timeout_s: float) -> str:
    """Shell snippet that exits 0 only when ``url`` answers; 77 when no probe tool exists."""
    seconds = max(1, math.ceil(timeout_s))
    quoted = shlex.quote(url)
    python_code = (
        f"import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout={seconds})"
    )
    return (
        "if command -v curl >/dev/null 2>&1; then "
        f"curl -fsS -o /dev/null --max-time {seconds} {quoted}; "
        "elif command -v wget >/dev/null 2>&1; then "
        f"wget -q -T {seconds} -O /dev/null {quoted}; "
        "elif command -v python3 >/dev/null 2>&1; then "
        f"python3 -c {shlex.quote(python_code)} {quoted}; "
        "else exit 77; fi"
    )


async def _probe_url(sandbox: Sandbox, url: str, *, timeout_s: float = _PROBE_TIMEOUT_S) -> int:
    result = await sandbox.exec(_http_probe_command(url, timeout_s), timeout_s=timeout_s + 5.0)
    if result.exit_code == 77:
        raise AssertionError(
            "sandbox has none of curl, wget or python3; cannot probe network reachability"
        )
    return result.exit_code


async def _assert_unreachable(sandbox: Sandbox, url: str) -> None:
    exit_code = await _probe_url(sandbox, url)
    if exit_code == 0:
        raise AssertionError(f"URL is reachable from the sandbox but must not be: {url}")


async def _assert_reachable(sandbox: Sandbox, url: str) -> None:
    exit_code = await _probe_url(sandbox, url)
    if exit_code != 0:
        raise AssertionError(f"URL is not reachable from the sandbox: {url} (exit {exit_code})")


# ---------------------------------------------------------------------------
# network cases
# ---------------------------------------------------------------------------


def _net01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if NetMode.OFF not in caps.net_modes:
        return f"target does not support network mode {NetMode.OFF.value!r}"
    if _observer(target) is None:
        return "target does not supply reachable_from_sandbox for the network canary"
    return None


async def _run_net01(target: ConformanceTarget) -> None:
    with _CanaryServer() as canary:
        async with _open_spec(
            target, _build_spec(target, "conf-net-01", network=NetMode.OFF)
        ) as sandbox:
            # Asked only now: a carrier may create the network it attaches on demand.
            hosts = await _all_observer_hosts(target)
            reached = [
                canary.url(host)
                for host in hosts
                if await _probe_url(sandbox, canary.url(host)) == 0
            ]
    if reached:
        raise AssertionError(
            "network 'off' sandbox reaches the host observer; URL is reachable from the "
            f"sandbox but must not be: {', '.join(reached)} (probed {', '.join(hosts)})"
        )


def _net02_mode(caps: SandboxCapabilities) -> NetMode | None:
    for mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
        if mode in caps.net_modes:
            return mode
    return None


def _net02_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if _net02_mode(caps) is None:
        return "target supports neither 'docs_whitelist' nor 'package_mirror'"
    if "hostname" not in caps.allowlist_kinds:
        return f"allowlist kinds {sorted(caps.allowlist_kinds)} do not include 'hostname'"
    if _observer(target) is None:
        return "target does not supply reachable_from_sandbox for the network canary"
    return None


async def _run_net02(target: ConformanceTarget) -> None:
    caps = _capabilities(target)
    mode = _net02_mode(caps)
    host = _observer(target)
    assert mode is not None and host is not None
    with _CanaryServer() as canary:
        url = canary.url(host)
        async with _open_spec(
            target,
            _build_spec(target, "conf-net-02-allow", network=mode, allow_hosts=(host,)),
        ) as sandbox:
            await _assert_reachable(sandbox, url)
        async with _open_spec(
            target, _build_spec(target, "conf-net-02-deny", network=mode)
        ) as sandbox:
            await _assert_unreachable(sandbox, url)


def _unsupported_policy(caps: SandboxCapabilities) -> NetworkPolicy | None:
    for mode in NetMode:
        if mode not in caps.net_modes:
            return NetworkPolicy(mode=mode)
    if "cidr" not in caps.allowlist_kinds:
        return NetworkPolicy(mode=NetMode.FULL, allow_cidrs=("10.0.0.0/8",))
    allowlist_mode = _net02_mode(caps)
    if "hostname" not in caps.allowlist_kinds and allowlist_mode is not None:
        return NetworkPolicy(mode=allowlist_mode, allow_hosts=("canary.invalid",))
    return None


def _net03_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    if _unsupported_policy(_capabilities(target)) is None:
        return "target declares every network policy kind it can be asked for"
    return None


async def _run_net03(target: ConformanceTarget) -> None:
    policy = _unsupported_policy(_capabilities(target))
    assert policy is not None
    factory = target.spec_factory
    assert factory is not None
    spec = replace(
        factory(
            "conf-net-03",
            network=policy.mode,
            max_lifetime_s=600,
        ),
        network=policy,
    )
    try:
        sandbox = await _provider(target).create(spec)
    except (PolicyNotEnforceable, CapabilityNotSupported):
        return
    await sandbox.destroy()
    raise AssertionError(f"create accepted an unsupported network policy: {policy!r}")


def _net04_applies(target: ConformanceTarget) -> str | None:
    return _linux_gate(target)


async def _run_net04(target: ConformanceTarget) -> None:
    async with _open_spec(
        target, _build_spec(target, "conf-net-04", network=NetMode.OFF)
    ) as sandbox:
        await _assert_unreachable(sandbox, _METADATA_URL)


def _net05_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if not caps.dynamic_network_policy:
        return "target does not declare dynamic_network_policy"
    missing = [m.value for m in (NetMode.OFF, NetMode.FULL) if m not in caps.net_modes]
    if missing:
        return f"target does not support network modes {missing}"
    if _observer(target) is None:
        return "target does not supply reachable_from_sandbox for the network canary"
    return None


async def _run_net05(target: ConformanceTarget) -> None:
    host = _observer(target)
    assert host is not None
    with _CanaryServer() as canary:
        url = canary.url(host)
        async with _open_spec(
            target, _build_spec(target, "conf-net-05", network=NetMode.OFF)
        ) as sandbox:
            await _assert_unreachable(sandbox, url)
            await sandbox.set_network(NetworkPolicy(mode=NetMode.FULL))
            await _assert_reachable(sandbox, url)
            await sandbox.set_network(NetworkPolicy(mode=NetMode.OFF))
            await _assert_unreachable(sandbox, url)


# ---------------------------------------------------------------------------
# isolation cases
# ---------------------------------------------------------------------------


def _iso01_applies(target: ConformanceTarget) -> str | None:
    return _posix_gate(target)


async def _run_iso01(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-iso-01-a")) as first:
        written = await first.exec(
            ["/bin/sh", "-c", "printf 'marker' > .conf-iso-marker && cat .conf-iso-marker"]
        )
        if written.exit_code != 0 or written.stdout != b"marker":
            raise AssertionError(
                "could not write the marker in the first sandbox: "
                f"exit {written.exit_code}, stdout {written.stdout!r}"
            )
    async with _open_spec(target, _build_spec(target, "conf-iso-01-b")) as second:
        probe = await second.exec(
            ["/bin/sh", "-c", "[ -e .conf-iso-marker ] && echo present || echo absent"]
        )
    if probe.stdout.strip() != b"absent":
        raise AssertionError(
            "a fresh sandbox sees the marker written by the previous instance "
            f"(probe output {probe.stdout!r})"
        )


def _iso02_applies(target: ConformanceTarget) -> str | None:
    return _linux_gate(target)


async def _run_iso02(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-iso-02")) as sandbox:
        uid = await sandbox.exec(["id", "-u"])
        if uid.exit_code != 0:
            raise AssertionError(f"cannot run 'id -u' in the sandbox: {uid.stderr!r}")
        if uid.stdout.strip() == b"0":
            raise AssertionError("sandbox runs as root")
        sudo = await sandbox.exec(["sudo", "-n", "true"], timeout_s=15.0)
        if sudo.exit_code == 0:
            raise AssertionError("passwordless sudo succeeds inside the sandbox")


_ENV_NAME_DENYLIST = "e2b|novita|task[_-]?id|trial[_-]?id|batch|model|eval|harbor|canary|reward"
_SECRET_VALUE_PATTERN = r"sk-[A-Za-z0-9]{8,}|BEGIN [A-Z ]*PRIVATE KEY"


def _iso03_applies(target: ConformanceTarget) -> str | None:
    return _linux_gate(target)


async def _run_iso03(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-iso-03")) as sandbox:
        names = await sandbox.exec(
            "env | cut -d= -f1 | grep -Ei " + shlex.quote(_ENV_NAME_DENYLIST) + " || true"
        )
        if names.stdout.strip():
            raise AssertionError(
                f"evaluation identity leaked into the sandbox environment: {names.stdout!r}"
            )
        values = await sandbox.exec(
            "env | grep -E " + shlex.quote(_SECRET_VALUE_PATTERN) + " || true"
        )
        if values.stdout.strip():
            raise AssertionError(
                f"a credential looks present in the sandbox environment: {values.stdout!r}"
            )
        files = await sandbox.exec(
            'for p in "$HOME/.aws/credentials" "$HOME/.netrc" '
            '"$HOME/.config/gh/hosts.yml" "$HOME/.novita" '
            '"/root/.aws/credentials" "/run/secrets" "/var/run/secrets"; do '
            'if [ -e "$p" ]; then echo "$p"; fi; done'
        )
        if files.stdout.strip():
            raise AssertionError(
                f"credential material is present in the sandbox filesystem: {files.stdout!r}"
            )


def _iso04_applies(target: ConformanceTarget) -> str | None:
    return _linux_gate(target)


def _mount_violations(output: bytes) -> list[str]:
    """Writable host mounts and container-socket leaks from a mount table dump."""
    violations: list[str] = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("SOCKET "):
            violations.append(stripped)
            continue
        source: str
        target: str
        options: str
        if " on " in stripped:
            source, _, rest = stripped.partition(" on ")
            target = rest.split(" (", 1)[0].strip()
            options = rest.partition(" (")[2].rstrip(")")
        else:
            fields = stripped.split()
            if len(fields) < 4:
                continue
            target, source, options = fields[0], fields[1], fields[3]
        if not source.startswith("/") or source.startswith("/dev/"):
            continue
        if target == "/" or target.startswith(("/proc", "/sys", "/dev", "/run")):
            continue
        if "rw" in options.split(","):
            violations.append(f"{source} mounted read-write at {target}")
    return violations


async def _run_iso04(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-iso-04")) as sandbox:
        probe = await sandbox.exec(
            "if command -v findmnt >/dev/null 2>&1; then "
            "findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS; else mount; fi; "
            "for s in /var/run/docker.sock /run/docker.sock; do "
            'if [ -S "$s" ]; then echo "SOCKET $s"; fi; done'
        )
    if probe.exit_code != 0:
        raise AssertionError(f"mount probe failed: {probe.stderr!r}")
    violations = _mount_violations(probe.stdout)
    if violations:
        raise AssertionError(
            "sandbox exposes host mounts or a container socket: " + "; ".join(violations)
        )


def _iso05_applies(target: ConformanceTarget) -> str | None:
    gate = _linux_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if not caps.enforces_limits:
        return "target declares no enforced resource limits"
    entry = _profile(target).invariants.get("I7")
    if entry is not None and entry.waived:
        reason = entry.reason or "no reason recorded"
        return f"isolation profile waives I7 (resource limits): {reason}"
    return None


async def _run_iso05(target: ConformanceTarget) -> None:
    caps = _capabilities(target)
    factory = target.spec_factory
    assert factory is not None
    spec = factory("conf-iso-05", network=NetMode.OFF, max_lifetime_s=600)
    checked = 0
    async with _open_spec(target, spec) as sandbox:
        limits = caps.enforces_limits
        if "cpu" in limits:
            cpu = await sandbox.exec("cat /sys/fs/cgroup/cpu.max 2>/dev/null || nproc")
            if cpu.exit_code == 0:
                checked += 1
                first = cpu.stdout.split()[0].decode() if cpu.stdout.split() else ""
                if first.isdigit():
                    quota = int(first) / 100_000
                    if quota > spec.resources.cpus:
                        raise AssertionError(
                            f"cpu quota {quota} exceeds the requested {spec.resources.cpus}"
                        )
                else:
                    nproc = int(cpu.stdout.split()[0])
                    if nproc > math.ceil(spec.resources.cpus):
                        raise AssertionError(
                            f"nproc {nproc} exceeds the requested {spec.resources.cpus} CPUs"
                        )
        if "memory" in limits:
            memory = await sandbox.exec("cat /sys/fs/cgroup/memory.max 2>/dev/null || true")
            value = memory.stdout.strip().decode()
            if memory.exit_code == 0 and value and value != "max":
                checked += 1
                requested = spec.resources.memory_mb * 1024 * 1024
                if int(value) > requested:
                    raise AssertionError(
                        f"memory.max {value} exceeds the requested {requested} bytes"
                    )
        if "pids" in limits:
            pids = await sandbox.exec("cat /sys/fs/cgroup/pids.max 2>/dev/null || true")
            value = pids.stdout.strip().decode()
            if pids.exit_code == 0 and value and value != "max":
                checked += 1
        if "disk" in limits:
            disk = await sandbox.exec("df -Pm . | tail -n 1")
            if disk.exit_code == 0 and disk.stdout.split():
                checked += 1
                available_mb = int(disk.stdout.split()[3])
                if available_mb > spec.resources.disk_mb:
                    raise AssertionError(
                        f"available disk {available_mb} MB exceeds the requested "
                        f"{spec.resources.disk_mb} MB"
                    )
    if checked == 0:
        raise AssertionError(
            f"no resource-limit probe ran for the declared limits {sorted(caps.enforces_limits)}"
        )


def _iso06_applies(target: ConformanceTarget) -> str | None:
    return _posix_gate(target)


async def _run_iso06(target: ConformanceTarget) -> None:
    token = f"evalspleef-canary-{uuid.uuid4().hex}"
    placement = ".canary/canary.md"
    async with _open_spec(target, _build_spec(target, "conf-iso-06")) as sandbox:
        with TemporaryDirectory(prefix="spleef-canary-") as tmp:
            source = Path(tmp) / "canary.md"
            source.write_text(f"canary placement\n{token}\n", encoding="utf-8")
            await sandbox.upload(source, f"/workspace/{placement}")
        scan = await sandbox.exec(f"grep -rl -- {shlex.quote(token)} . || true")
        found = _scan_paths(scan.stdout)
        if found != [placement]:
            raise AssertionError(f"canary scan should find only {placement!r}, found {found!r}")
        await sandbox.exec(f"printf '%s\\n' {shlex.quote(token)} > stray.txt")
        scan = await sandbox.exec(f"grep -rl -- {shlex.quote(token)} . || true")
        found = _scan_paths(scan.stdout)
        if found != sorted([placement, "stray.txt"]):
            raise AssertionError(f"canary scan did not detect the injected leak: found {found!r}")
        await sandbox.exec("rm -f stray.txt")
        scan = await sandbox.exec(f"grep -rl -- {shlex.quote(token)} . || true")
        found = _scan_paths(scan.stdout)
        if found != [placement]:
            raise AssertionError(
                f"canary scan after cleanup should find only {placement!r}, found {found!r}"
            )


def _scan_paths(output: bytes) -> list[str]:
    paths = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        path = line.strip()
        if path.startswith("./"):
            path = path[2:]
        if path:
            paths.append(path)
    return sorted(paths)


# ---------------------------------------------------------------------------
# concurrency and fault cases
# ---------------------------------------------------------------------------


def _conc01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if caps.max_concurrency is None:
        return "target declares no max_concurrency"
    if target.fake:
        return "in-process double does not model the vendor concurrency limit"
    return None


async def _run_conc01(target: ConformanceTarget) -> None:
    caps = _capabilities(target)
    limit = caps.max_concurrency
    assert limit is not None
    provider = _provider(target)
    trial_ids = [f"conf-conc-{index}" for index in range(limit + 1)]
    specs = [_build_spec(target, trial_id, max_lifetime_s=900) for trial_id in trial_ids]
    results = await asyncio.gather(
        *(provider.create(spec) for spec in specs), return_exceptions=True
    )
    created = [item for item in results if isinstance(item, Sandbox)]
    failures = [item for item in results if isinstance(item, BaseException)]
    try:
        if len(created) > limit:
            raise AssertionError(
                f"provider created {len(created)} concurrent sandboxes, max_concurrency is {limit}"
            )
        unexpected = [failure for failure in failures if not isinstance(failure, QuotaExceeded)]
        if unexpected:
            raise AssertionError(
                "concurrent creates beyond max_concurrency raised unexpected errors: "
                + "; ".join(repr(item) for item in unexpected)
            )
        if not failures:
            raise AssertionError(
                "provider accepted more creates than max_concurrency without rejecting one"
            )
    finally:
        await asyncio.gather(*(sandbox.destroy() for sandbox in created), return_exceptions=True)


def _fail01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    if _injector(target, "lose_sandbox") is None:
        return "target supplies no 'lose_sandbox' fault injector"
    return None


async def _run_fail01(target: ConformanceTarget) -> None:
    injector = _injector(target, "lose_sandbox")
    assert injector is not None
    async with _open_spec(target, _build_spec(target, "conf-fail-01")) as sandbox:
        await injector(sandbox)
        try:
            await sandbox.exec(["true"])
        except SandboxLost:
            return
    raise AssertionError("using a vanished sandbox did not raise SandboxLost")


def _fail02_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    if _injector(target, "fail_create") is None:
        return "target supplies no 'fail_create' fault injector"
    return None


async def _run_fail02(target: ConformanceTarget) -> None:
    injector = _injector(target, "fail_create")
    assert injector is not None
    await injector()
    provider = _provider(target)
    spec = _build_spec(target, "conf-fail-02-a")
    try:
        sandbox = await provider.create(spec)
    except SpleefError as exc:
        if not isinstance(exc, SandboxCreateError):
            raise AssertionError(
                f"injected create failure raised {type(exc).__name__}, expected SandboxCreateError"
            ) from exc
    else:
        await sandbox.destroy()
        raise AssertionError("injected create failure did not raise")
    retried = await provider.create(_build_spec(target, "conf-fail-02-b"))
    try:
        probe = await retried.exec(["true"])
    finally:
        await retried.destroy()
    if probe.exit_code != 0:
        raise AssertionError(f"sandbox created on retry is not usable: {probe.stderr!r}")


# ---------------------------------------------------------------------------
# optional capability cases
# ---------------------------------------------------------------------------


def _snap01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if caps.snapshot == "none":
        return "target declares snapshot support 'none'"
    return None


async def _run_snap01(target: ConformanceTarget) -> None:
    provider = _provider(target)
    spec = _build_spec(target, "conf-snap-01")
    sandbox = await provider.create(spec)
    try:
        write = await sandbox.exec(["sh", "-c", "printf 'snapshot-body' > .snap-marker"])
        if write.exit_code != 0:
            raise AssertionError(f"cannot write the snapshot marker: {write.stderr!r}")
        snapshot_id = await sandbox.snapshot("conf-snap-01")
    finally:
        await sandbox.destroy()
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise AssertionError(f"snapshot() returned an empty id: {snapshot_id!r}")
    restored = await provider.create_from_snapshot(
        snapshot_id, _build_spec(target, "conf-snap-01-restored")
    )
    try:
        probe = await restored.exec(["cat", ".snap-marker"], timeout_s=30.0)
    finally:
        await restored.destroy()
    if probe.exit_code != 0 or probe.stdout != b"snapshot-body":
        raise AssertionError(
            "restored sandbox lost the snapshot content: "
            f"exit {probe.exit_code}, stdout {probe.stdout!r}"
        )


def _snap02_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if not caps.snapshot_fanout:
        return "target does not declare snapshot_fanout"
    return None


async def _run_snap02(target: ConformanceTarget) -> None:
    provider = _provider(target)
    sandbox = await provider.create(_build_spec(target, "conf-snap-02"))
    try:
        await sandbox.exec(["sh", "-c", "printf 'seed' > .snap-marker"])
        snapshot_id = await sandbox.snapshot("conf-snap-02")
    finally:
        await sandbox.destroy()
    first = await provider.create_from_snapshot(snapshot_id, _build_spec(target, "conf-snap-02-a"))
    second = await provider.create_from_snapshot(snapshot_id, _build_spec(target, "conf-snap-02-b"))
    try:
        await first.exec(["sh", "-c", "printf 'diverged' > .snap-marker"])
        probe = await second.exec(["cat", ".snap-marker"])
        if probe.stdout != b"seed":
            raise AssertionError(
                f"sandboxes restored from one snapshot share state: {probe.stdout!r}"
            )
    finally:
        await first.destroy()
        await second.destroy()


def _gui01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if caps.gui == "none":
        return "target declares GUI support 'none'"
    return None


async def _run_gui01(target: ConformanceTarget) -> None:
    async with _open_spec(target, _build_spec(target, "conf-gui-01")) as sandbox:
        endpoint = await sandbox.gui_endpoint()
    if not isinstance(endpoint, str) or not endpoint:
        raise AssertionError(f"gui_endpoint() returned {endpoint!r}")
    parts = urlsplit(endpoint)
    if parts.scheme not in ("vnc", "rdp", "http", "https", "ws", "wss"):
        raise AssertionError(f"GUI endpoint has an unknown scheme: {endpoint!r}")
    if parts.hostname is None:
        raise AssertionError(f"GUI endpoint has no host: {endpoint!r}")
    port = parts.port
    if port is None:
        raise AssertionError(f"GUI endpoint has no port: {endpoint!r}")
    # The SPI exposes no screenshot surface, so the case proves the endpoint speaks:
    # a VNC server announces its protocol version, other GUI protocols must accept TCP.
    if parts.scheme == "vnc":
        banner = await _tcp_banner(parts.hostname, port)
        if not banner.startswith(b"RFB "):
            raise AssertionError(f"VNC endpoint did not send an RFB banner: {banner!r}")
    else:
        await _tcp_banner(parts.hostname, port)


async def _tcp_banner(host: str, port: int, *, timeout_s: float = 5.0) -> bytes:
    async def _read() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            return await reader.read(64)
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.wait_for(_read(), timeout=timeout_s)


def _port01_applies(target: ConformanceTarget) -> str | None:
    gate = _sandbox_gate(target)
    if gate is not None:
        return gate
    caps = _capabilities(target)
    if not caps.port_expose:
        return "target does not declare port_expose"
    return None


async def _run_port01(target: ConformanceTarget) -> None:
    port = _free_port()
    async with _open_spec(target, _build_spec(target, "conf-port-01")) as sandbox:
        await sandbox.exec(f"(python3 -m http.server {port} >/dev/null 2>&1 </dev/null &)")
        url = await sandbox.expose_port(port)
        try:
            status, body = await _http_get_from_host(url)
        finally:
            await sandbox.exec(f"pkill -f 'http.server {port}' || true", timeout_s=15.0)
    if status != 200:
        raise AssertionError(f"exposed port answered HTTP {status}")
    if b"Directory listing" not in body:
        raise AssertionError("exposed port did not serve the sandbox HTTP server")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


async def _http_get_from_host(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
    async def _fetch() -> tuple[int, bytes]:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise AssertionError(f"exposed port URL has no host: {url!r}")
        tls = parts.scheme in ("https", "wss")
        port = parts.port or (443 if tls else 80)
        context = ssl.create_default_context() if tls else None
        reader, writer = await asyncio.open_connection(
            host, port, ssl=context, server_hostname=host if tls else None
        )
        try:
            path = parts.path or "/"
            if parts.query:
                path = f"{path}?{parts.query}"
            request = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
            writer.write(request.encode())
            await writer.drain()
            payload = await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
        head, _, body = payload.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].split()
        if len(status_line) < 2 or not status_line[1].isdigit():
            raise AssertionError(f"malformed HTTP response: {head[:80]!r}")
        return int(status_line[1]), body

    return await asyncio.wait_for(_fetch(), timeout=timeout_s)


# ---------------------------------------------------------------------------
# case table
# ---------------------------------------------------------------------------

CASES: tuple[Case, ...] = (
    Case(
        id="C-NET-01",
        kind="sandbox",
        title="network mode 'off' cannot reach the network canary",
        applies=_net01_applies,
        run=_run_net01,
    ),
    Case(
        id="C-NET-02",
        kind="sandbox",
        title="hostname allowlist reaches the canary and blocks it otherwise",
        applies=_net02_applies,
        run=_run_net02,
    ),
    Case(
        id="C-NET-03",
        kind="sandbox",
        title="an unsupported network policy is rejected at create",
        applies=_net03_applies,
        run=_run_net03,
    ),
    Case(
        id="C-NET-04",
        kind="sandbox",
        title="the cloud metadata address is unreachable",
        applies=_net04_applies,
        run=_run_net04,
    ),
    Case(
        id="C-NET-05",
        kind="sandbox",
        title="runtime network policy changes take effect",
        applies=_net05_applies,
        run=_run_net05,
    ),
    Case(
        id="C-ISO-01",
        kind="sandbox",
        title="a fresh instance cannot see the previous instance's marker",
        applies=_iso01_applies,
        run=_run_iso01,
    ),
    Case(
        id="C-ISO-02",
        kind="sandbox",
        title="commands run non-root and passwordless sudo fails",
        applies=_iso02_applies,
        run=_run_iso02,
    ),
    Case(
        id="C-ISO-03",
        kind="sandbox",
        title="no evaluation identity or credentials leak into env or files",
        applies=_iso03_applies,
        run=_run_iso03,
    ),
    Case(
        id="C-ISO-04",
        kind="sandbox",
        title="no writable host mounts and no container socket",
        applies=_iso04_applies,
        run=_run_iso04,
    ),
    Case(
        id="C-ISO-05",
        kind="sandbox",
        title="declared resource limits are enforced",
        applies=_iso05_applies,
        run=_run_iso05,
    ),
    Case(
        id="C-ISO-06",
        kind="sandbox",
        title="canary placement scan closes the injection loop",
        applies=_iso06_applies,
        run=_run_iso06,
    ),
    Case(
        id="C-CONC-01",
        kind="sandbox",
        title="concurrent sandboxes never exceed max_concurrency",
        applies=_conc01_applies,
        run=_run_conc01,
    ),
    Case(
        id="C-FAIL-01",
        kind="sandbox",
        title="using a vanished sandbox raises SandboxLost",
        applies=_fail01_applies,
        run=_run_fail01,
    ),
    Case(
        id="C-FAIL-02",
        kind="sandbox",
        title="an injected create failure raises SandboxCreateError and retry works",
        applies=_fail02_applies,
        run=_run_fail02,
    ),
    Case(
        id="C-SNAP-01",
        kind="sandbox",
        title="a snapshot restores the filesystem content",
        applies=_snap01_applies,
        run=_run_snap01,
    ),
    Case(
        id="C-SNAP-02",
        kind="sandbox",
        title="one snapshot restores to independent instances",
        applies=_snap02_applies,
        run=_run_snap02,
    ),
    Case(
        id="C-GUI-01",
        kind="sandbox",
        title="the GUI endpoint answers on its protocol",
        applies=_gui01_applies,
        run=_run_gui01,
    ),
    Case(
        id="C-PORT-01",
        kind="sandbox",
        title="an exposed port serves the sandbox listener",
        applies=_port01_applies,
        run=_run_port01,
    ),
)
