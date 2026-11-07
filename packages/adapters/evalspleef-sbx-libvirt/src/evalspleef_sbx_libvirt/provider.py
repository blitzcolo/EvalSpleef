"""libvirt (QEMU/KVM) sandbox provider (TECH-DESIGN section 7.4.5).

One domain per trial, booted from a per-trial qcow2 overlay whose backing file is
the immutable golden image; deleting the overlay is the reset. Domains are
defined and controlled exclusively through the ``virsh`` CLI (no libvirt Python
binding). Commands run over SSH with asyncssh (streaming output, exit codes);
the qemu-guest-agent ``guest-exec`` path is a documented, non-streaming
fallback. Guests may be Linux or Windows, on x86_64 or arm64 hosts according to
the provider configuration.

Network policy mapping (FR-SBX-03/07): ``off`` attaches an isolated libvirt
network with no forwarding plus an nwfilter that drops egress; ``full`` uses the
configured forward network; ``docs_whitelist``/``package_mirror`` are offered
only when an egress proxy is configured and the policy carries ``via_proxy``,
and then attach the proxy network with the allow-proxy nwfilter. Anything else
is rejected at create with ``PolicyNotEnforceable``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import platform
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import asyncssh

from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SpleefError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    GuiSupport,
    NetworkPolicy,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from .isolation import isolation_profile, probe_script
from .sandbox import LibvirtSandbox

_LABEL_NS = "urn:evalspleef:labels"
_HOST_ARCH = {
    "aarch64": Arch.ARM64,
    "arm64": Arch.ARM64,
    "x86_64": Arch.X86_64,
    "amd64": Arch.X86_64,
}
_GUEST_ARCH = {Arch.ARM64: "aarch64", Arch.X86_64: "x86_64"}
_DOMAIN_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")
_VIA_PROXY_MODES = (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR)


class _EmptySecrets:
    """Placeholder used when a caller constructs a provider without a resolver."""

    def resolve(self, ref: Any) -> str:  # pragma: no cover - the adapter resolves nothing
        raise RuntimeError(f"the libvirt provider resolves no secrets: {ref!r}")


class LibvirtProvider(SandboxProvider):
    """Sandbox provider backed by local libvirt/QEMU through the virsh CLI."""

    name = "libvirt"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(instance, config, secrets or _EmptySecrets())
        self._uri = str(config.get("uri", "qemu:///system"))
        self._virsh_bin = str(config.get("virsh_bin", "virsh"))
        self._qemu_img_bin = str(config.get("qemu_img_bin", "qemu-img"))
        self._overlay_dir = Path(config.get("overlay_dir", "/var/lib/libvirt/images/spleef"))
        self._network_off = str(config.get("network_off", "spleef-off"))
        self._network_full = str(config.get("network_full", "default"))
        network_proxy = config.get("network_proxy")
        self._network_proxy = None if network_proxy is None else str(network_proxy)
        egress_proxy = config.get("egress_proxy")
        self._egress_proxy = None if egress_proxy is None else str(egress_proxy)
        self._nwfilter_off = str(config.get("nwfilter_off", "spleef-off"))
        self._nwfilter_proxy = str(config.get("nwfilter_proxy", "spleef-via-proxy"))
        self._guest_os = _os_families(config.get("guest_os", ["linux", "windows"]))
        self._arches = _arches(
            config.get("arches", [_HOST_ARCH.get(platform.machine(), Arch.ARM64)])
        )
        self._gui: GuiSupport = _gui_support(config.get("gui"), self._guest_os)
        self._ssh_host = _optional_str(config.get("ssh_host"))
        self._ssh_port = _config_int(config, "ssh_port", 22)
        self._ssh_user = str(config.get("ssh_user", "agent"))
        self._ssh_key_file = _optional_str(config.get("ssh_key_file"))
        self._ssh_known_hosts = _optional_str(config.get("ssh_known_hosts"))
        self._ssh_connect_timeout_s = _config_float(config, "ssh_connect_timeout_s", 10.0)
        self._startup_timeout_s = _config_float(config, "startup_timeout_s", 180.0)
        self._wait_for_ssh = _config_bool(config, "wait_for_ssh", True)
        self._guest_agent_fallback = _config_bool(config, "guest_agent_fallback", True)
        self._max_concurrency = _config_int(config, "max_concurrency", 4)
        self._max_lifetime_s = _config_int(config, "max_lifetime_s", 7200)
        self._vnc_port = _config_int(config, "vnc_port", 5900)
        self._rdp_port = _config_int(config, "rdp_port", 3389)
        self._live: set[str] = set()
        self._sandboxes: dict[str, LibvirtSandbox] = {}
        self._pending = 0
        self._fail_next: list[type[SpleefError] | SpleefError] = []

    # -- connections ------------------------------------------------------------

    @property
    def ssh_user(self) -> str:
        return self._ssh_user

    @property
    def ssh_key_file(self) -> str | None:
        return self._ssh_key_file

    @property
    def ssh_known_hosts(self) -> str | None:
        return self._ssh_known_hosts

    @property
    def ssh_connect_timeout_s(self) -> float:
        return self._ssh_connect_timeout_s

    @property
    def guest_agent_fallback(self) -> bool:
        return self._guest_agent_fallback

    def gui_port(self, support: str) -> int:
        return self._rdp_port if support == "rdp" else self._vnc_port

    # -- capabilities and profile ----------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        net_modes = {NetMode.OFF, NetMode.FULL}
        if self._egress_proxy and self._network_proxy:
            net_modes.update(_VIA_PROXY_MODES)
        return SandboxCapabilities(
            os_families=self._guest_os,
            arches=self._arches,
            isolation=Isolation.L3,
            net_modes=frozenset(net_modes),
            allowlist_kinds=frozenset(),
            dynamic_network_policy=False,
            streaming=True,
            stdin=True,
            snapshot="none",
            snapshot_fanout=False,
            pause_resume=False,
            gui=self._gui,
            gpu=False,
            port_expose=False,
            host_mounts=False,
            enforces_limits=frozenset({"cpu", "memory"}),
            max_concurrency=self._max_concurrency,
            max_lifetime_s=self._max_lifetime_s,
            max_resources=None,
            typical_start_s=60.0,
            reset_cost_s=5.0,
        )

    def isolation_profile(self) -> IsolationProfile:
        return isolation_profile()

    async def preflight(self) -> dict[str, Any]:
        code, stdout, stderr = await self._virsh("version", timeout_s=30.0)
        if code != 0:
            raise SpleefError(
                f"libvirt preflight failed: {stderr.decode(errors='replace').strip() or code}"
            )
        version_line = stdout.decode(errors="replace").splitlines()
        return {
            "provider": f"{self.name}/{self.instance}",
            "uri": self._uri,
            "virsh_version": version_line[0] if version_line else "",
            "overlay_dir": str(self._overlay_dir),
            "overlay_dir_exists": self._overlay_dir.is_dir(),
            "guest_os": sorted(item.value for item in self._guest_os),
            "arches": sorted(item.value for item in self._arches),
            "gui": self._gui,
            "ssh_host": self._ssh_host or "discovered",
            "ssh_port": self._ssh_port,
            "egress_proxy": self._egress_proxy,
            "max_concurrency": self._max_concurrency,
        }

    # -- test-side controls (used by the conformance injectors) -----------------

    def fail_next_creates(self, n: int, error: type[SpleefError] | SpleefError) -> None:
        """Make the next ``n`` create calls raise ``error`` (a class or an instance)."""
        self._fail_next.extend([error] * n)

    async def lose(self, sandbox_id: str) -> None:
        """Simulate the hypervisor reclaiming a live domain."""
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is not None:
            sandbox.mark_lost()
        self._live.discard(sandbox_id)
        await self._destroy_domain(sandbox_id, sandbox.overlay_path if sandbox else None)

    async def expire_live_sandboxes(self) -> None:
        """Destroy every live sandbox, mirroring a TTL expiry sweep."""
        for sandbox_id in list(self._sandboxes):
            await self.lose(sandbox_id)

    async def close(self) -> None:
        """Release cached SSH connections; does not destroy domains."""
        for sandbox in list(self._sandboxes.values()):
            await sandbox.close()

    # -- SandboxProvider ---------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> Sandbox:
        if self._fail_next:
            injected = self._fail_next.pop(0)
            if isinstance(injected, type):
                raise injected()
            raise injected
        self._validate_spec(spec)
        if len(self._live) + self._pending >= self._max_concurrency:
            raise QuotaExceeded(
                f"libvirt provider {self.instance!r} is at max_concurrency {self._max_concurrency}",
                detail={"provider": self.instance, "max_concurrency": self._max_concurrency},
            )
        self._pending += 1
        name = ""
        overlay: Path | None = None
        ssh_host: str | None = None
        try:
            golden = Path(spec.template.ref)
            if not await asyncio.to_thread(golden.is_file):
                raise SandboxCreateError(
                    f"golden image {golden} does not exist",
                    detail={"template": spec.template},
                )
            try:
                self._overlay_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SandboxCreateError(f"cannot create overlay directory: {exc}") from exc
            name = self._domain_name(spec)
            overlay = self._overlay_dir / f"{name}.qcow2"
            code, _, stderr = await self._run(
                [
                    self._qemu_img_bin,
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(golden),
                    str(overlay),
                ],
                timeout_s=120.0,
            )
            if code != 0:
                raise SandboxCreateError(
                    f"qemu-img create failed: {_text(stderr)}",
                    detail={"template": spec.template},
                )
            labels = self._labels(spec, overlay)
            network, nwfilter = self._resolved_network(spec.network)
            now = int(time.time())
            xml = domain_xml(
                name=name,
                spec=spec,
                overlay=overlay,
                labels=labels,
                network=network,
                nwfilter=nwfilter,
                created_at=now,
                expires_at=now + spec.max_lifetime_s,
                vnc_port=self._vnc_port if spec.gui else None,
                rdp_port=self._rdp_port if spec.gui else None,
            )
            code, _, stderr = await self._virsh(
                "define", "-", input=xml.encode("utf-8"), timeout_s=60.0
            )
            if code != 0:
                raise SandboxCreateError(f"virsh define failed: {_text(stderr)}")
            code, _, stderr = await self._virsh("start", name, timeout_s=120.0)
            if code != 0:
                raise SandboxCreateError(f"virsh start failed: {_text(stderr)}")
            ssh_host = await self._resolve_ssh_host(name)
        except BaseException:
            if name:
                await self._destroy_domain(name, overlay)
            raise
        finally:
            self._pending -= 1
        assert ssh_host is not None  # set by _resolve_ssh_host on the success path
        self._live.add(name)
        sandbox = LibvirtSandbox(
            self,
            name,
            spec,
            overlay_path=overlay,
            ssh_host=ssh_host,
            ssh_port=self._ssh_port,
        )
        self._sandboxes[name] = sandbox
        return sandbox

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """All defined domains whose metadata labels contain every given pair."""
        code, stdout, stderr = await self._virsh("list", "--all", "--name", timeout_s=60.0)
        if code != 0:
            raise SpleefError(f"virsh list failed: {_text(stderr)}")
        entries: list[dict[str, Any]] = []
        for name in [line.strip() for line in _text(stdout).splitlines() if line.strip()]:
            code, xml_out, _ = await self._virsh("dumpxml", name, timeout_s=30.0)
            if code != 0:
                continue
            entry = _summarise(name, xml_out)
            if entry is None:
                continue
            if all(entry["labels"].get(key) == value for key, value in labels.items()):
                entries.append(entry)
        return entries

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        """Destroy matching orphan domains older than ``older_than_s`` or past their TTL."""
        now = time.time()
        reaped = 0
        for entry in await self.list(labels):
            created = float(entry.get("created_at") or 0)
            expires_at = float(entry["labels"].get("spleef.expires_at") or 0)
            past_age = older_than_s <= 0 or (created > 0 and (now - created) >= older_than_s)
            past_ttl = expires_at > 0 and expires_at <= now
            if not (past_age or past_ttl):
                continue
            sandbox = self._sandboxes.pop(entry["id"], None)
            if sandbox is not None:
                sandbox.mark_destroyed()
                await sandbox.close()
            self._live.discard(entry["id"])
            overlay = entry["labels"].get("spleef.overlay")
            await self._destroy_domain(entry["id"], Path(overlay) if overlay else None)
            reaped += 1
        return reaped

    async def release_sandbox(self, sandbox: LibvirtSandbox) -> None:
        """Idempotent destroy: stop, undefine and delete the overlay."""
        self._live.discard(sandbox.id)
        self._sandboxes.pop(sandbox.id, None)
        await sandbox.close()
        await self._destroy_domain(sandbox.id, sandbox.overlay_path)

    async def guest_agent_command(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One qemu-agent-command round trip (used by the exec fallback)."""
        code, stdout, stderr = await self._virsh(
            "qemu-agent-command", name, json.dumps(dict(payload)), timeout_s=30.0
        )
        if code != 0:
            raise SpleefError(f"qemu-agent-command failed: {_text(stderr)}")
        try:
            reply = json.loads(_text(stdout) or "{}")
        except json.JSONDecodeError as exc:
            raise SpleefError(f"qemu-agent-command returned invalid JSON: {exc}") from exc
        result = reply.get("return")
        return result if isinstance(result, dict) else {}

    # -- guest probes ------------------------------------------------------------

    async def run_probes(self, sandbox: Sandbox) -> dict[str, Any]:
        """Run the OS-family isolation probe set through exec stdin."""
        script = probe_script(sandbox.spec.os)
        if sandbox.spec.os is OSFamily.WINDOWS:
            command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"]
        else:
            command = ["sh", "-s"]
        result = await sandbox.exec(command, stdin=script.encode("utf-8"), timeout_s=120.0)
        lines = _text(result.stdout).splitlines()
        checks = [line for line in lines if line.startswith("CHECK ")]
        verdict = next((line for line in reversed(lines) if line.startswith("RESULT ")), "")
        return {
            "exit_code": result.exit_code,
            "passed": verdict.strip() == "RESULT pass",
            "checks": checks,
            "stdout": _text(result.stdout),
            "stderr": _text(result.stderr),
        }

    # -- validation --------------------------------------------------------------

    def _validate_spec(self, spec: SandboxSpec) -> None:
        caps = self.capabilities()
        if spec.os not in caps.os_families:
            raise CapabilityNotSupported(
                f"libvirt guest OS {spec.os.value!r} is not configured; supported: "
                f"{sorted(item.value for item in caps.os_families)}"
            )
        if spec.arch not in caps.arches:
            raise CapabilityNotSupported(
                f"libvirt cannot run arch {spec.arch.value!r}; configured: "
                f"{sorted(item.value for item in caps.arches)}"
            )
        if spec.template.kind != "qcow2":
            raise CapabilityNotSupported(
                f"libvirt needs a qcow2 template, got {spec.template.kind!r}"
            )
        if spec.resources.gpus > 0:
            raise CapabilityNotSupported("libvirt provider does not support GPUs")
        if spec.resources.cpus <= 0 or spec.resources.memory_mb <= 0:
            raise PolicyNotEnforceable("cpu and memory requests must be positive")
        if spec.gui:
            if caps.gui == "none":
                raise CapabilityNotSupported("GUI support is disabled for this provider")
            if caps.gui == "rdp" and spec.os is not OSFamily.WINDOWS:
                raise CapabilityNotSupported(
                    f"gui {caps.gui!r} is only available for windows guests"
                )
        if spec.max_lifetime_s > self._max_lifetime_s:
            raise PolicyNotEnforceable(
                f"requested lifetime {spec.max_lifetime_s}s exceeds the provider ceiling "
                f"{self._max_lifetime_s}s"
            )
        self._resolved_network(spec.network)

    def _resolved_network(self, policy: NetworkPolicy) -> tuple[str, str | None]:
        if policy.mode == NetMode.OFF:
            if policy.allow_hosts or policy.allow_cidrs:
                raise PolicyNotEnforceable("an off-network sandbox cannot honour an allowlist")
            return self._network_off, self._nwfilter_off
        if policy.mode == NetMode.FULL:
            if policy.allow_hosts or policy.allow_cidrs:
                raise PolicyNotEnforceable(
                    "libvirt cannot enforce host/cidr allowlists on a full network; use "
                    "docs_whitelist/package_mirror with via_proxy"
                )
            return self._network_full, None
        if policy.mode in _VIA_PROXY_MODES:
            if not self._egress_proxy or not self._network_proxy:
                raise PolicyNotEnforceable(
                    f"network mode {policy.mode.value!r} needs an egress proxy: configure "
                    "'egress_proxy' and 'network_proxy' and pass policy.via_proxy"
                )
            if not policy.via_proxy:
                raise PolicyNotEnforceable(
                    f"network mode {policy.mode.value!r} requires policy.via_proxy"
                )
            if policy.allow_cidrs:
                raise PolicyNotEnforceable(
                    "libvirt nwfilter allowlisting supports the proxy address only, not cidrs"
                )
            return self._network_proxy, self._nwfilter_proxy
        raise CapabilityNotSupported(f"network mode {policy.mode.value!r} is not supported")

    # -- helpers -----------------------------------------------------------------

    def _domain_name(self, spec: SandboxSpec) -> str:
        slug = _DOMAIN_NAME_RE.sub("-", spec.trial_id).strip("-.") or "trial"
        return f"spleef-{slug[:48]}-{uuid.uuid4().hex[:8]}"

    def _labels(self, spec: SandboxSpec, overlay: Path) -> dict[str, str]:
        now = int(time.time())
        labels = {key: str(value) for key, value in spec.labels.items()}
        labels["spleef.provider"] = self.instance
        labels["spleef.created_at"] = str(now)
        labels["spleef.expires_at"] = str(now + spec.max_lifetime_s)
        labels["spleef.overlay"] = str(overlay)
        labels["spleef.template_digest"] = spec.template.digest
        return labels

    async def _resolve_ssh_host(self, name: str) -> str:
        if self._ssh_host is not None and not self._wait_for_ssh:
            return self._ssh_host
        deadline = time.monotonic() + self._startup_timeout_s
        attempted: list[str] = []
        while True:
            candidate = self._ssh_host
            if candidate is None:
                candidate = await self._guest_address(name)
            if candidate is not None:
                attempted.append(candidate)
                if not self._wait_for_ssh:
                    return candidate
                try:
                    connection = await asyncssh.connect(
                        candidate,
                        self._ssh_port,
                        username=self._ssh_user,
                        known_hosts=self._ssh_known_hosts,
                        encoding=None,
                        connect_timeout=min(5.0, max(1.0, deadline - time.monotonic())),
                        **(
                            {"client_keys": [self._ssh_key_file]}
                            if self._ssh_key_file is not None
                            else {}
                        ),
                    )
                except (TimeoutError, OSError, asyncssh.Error):
                    pass
                else:
                    connection.close()
                    await connection.wait_closed()
                    return candidate
            if time.monotonic() >= deadline:
                raise SandboxCreateError(
                    "the guest SSH endpoint did not become reachable in "
                    f"{self._startup_timeout_s:.0f}s (tried {sorted(set(attempted)) or 'no address'})"
                )
            await asyncio.sleep(0.2)

    async def _guest_address(self, name: str) -> str | None:
        code, stdout, _ = await self._virsh("domifaddr", name, "--source", "lease", timeout_s=30.0)
        if code != 0:
            return None
        for line in _text(stdout).splitlines():
            fields = line.split()
            for index, field in enumerate(fields):
                if field in ("ipv4", "ipv6") and index + 1 < len(fields):
                    return fields[index + 1].split("/", 1)[0]
        return None

    async def _destroy_domain(self, name: str, overlay: Path | None) -> None:
        await self._virsh("destroy", name, timeout_s=60.0)
        await self._virsh("undefine", name, timeout_s=60.0)
        if overlay is not None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(overlay.unlink, missing_ok=True)

    async def _virsh(
        self,
        *args: str,
        input: bytes | None = None,
        timeout_s: float = 60.0,
    ) -> tuple[int, bytes, bytes]:
        return await self._run(
            [self._virsh_bin, "-c", self._uri, *args], input=input, timeout_s=timeout_s
        )

    async def _run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout_s: float,
    ) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return 127, b"", str(exc).encode()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), timeout=timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, b"", b"command timed out"
        return int(process.returncode or 0), stdout, stderr


def domain_xml(
    *,
    name: str,
    spec: SandboxSpec,
    overlay: Path,
    labels: Mapping[str, str],
    network: str,
    nwfilter: str | None,
    created_at: int,
    expires_at: int,
    vnc_port: int | None = None,
    rdp_port: int | None = None,
) -> str:
    """Build the libvirt domain XML for one trial."""
    windows = spec.os is OSFamily.WINDOWS
    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = name
    metadata = ET.SubElement(domain, "metadata")
    labels_element = ET.SubElement(
        metadata,
        f"{{{_LABEL_NS}}}labels",
        {
            "provider": str(labels.get("spleef.provider", "")),
            "created_at": str(created_at),
            "expires_at": str(expires_at),
        },
    )
    for key, value in sorted(labels.items()):
        ET.SubElement(labels_element, f"{{{_LABEL_NS}}}label", {"name": key, "value": str(value)})
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(spec.resources.memory_mb)
    ET.SubElement(domain, "vcpu", {"placement": "static"}).text = str(
        max(1, math.ceil(spec.resources.cpus))
    )
    machine = "q35" if spec.arch is Arch.X86_64 else "virt"
    os_element = ET.SubElement(domain, "os")
    ET.SubElement(
        os_element, "type", {"arch": _GUEST_ARCH[spec.arch], "machine": machine}
    ).text = "hvm"
    ET.SubElement(os_element, "boot", {"dev": "hd"})
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    if spec.arch is Arch.X86_64:
        ET.SubElement(features, "apic")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk, "source", {"file": str(overlay)})
    if windows:
        ET.SubElement(disk, "target", {"dev": "sda", "bus": "sata"})
    else:
        ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})
    interface = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(interface, "source", {"network": network})
    ET.SubElement(interface, "model", {"type": "virtio"})
    if nwfilter:
        ET.SubElement(interface, "filterref", {"filter": nwfilter})
    if spec.gui:
        if windows:
            ET.SubElement(
                devices,
                "graphics",
                {"type": "rdp", "port": str(rdp_port or 3389), "listen": "127.0.0.1"},
            )
        else:
            ET.SubElement(
                devices,
                "graphics",
                {"type": "vnc", "port": str(vnc_port or 5900), "listen": "127.0.0.1"},
            )
    ET.SubElement(devices, "console", {"type": "pty"})
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(domain, encoding="unicode")


def _summarise(name: str, xml_bytes: bytes) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    labels: dict[str, str] = {}
    metadata = root.find("metadata")
    if metadata is not None:
        labels_element = metadata.find(f"{{{_LABEL_NS}}}labels")
        if labels_element is not None:
            for item in labels_element.findall(f"{{{_LABEL_NS}}}label"):
                key = item.get("name")
                if key is not None:
                    labels[key] = item.get("value", "")
    return {
        "id": name,
        "labels": labels,
        "state": "unknown",
        "created_at": int(labels.get("spleef.created_at") or 0),
    }


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_float(config: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_bool(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _os_families(raw: Any) -> frozenset[OSFamily]:
    values = [raw] if isinstance(raw, str) else list(raw or [])
    families = {OSFamily(str(item).lower()) for item in values}
    if not families:
        raise ValueError("guest_os must name at least one OS family")
    return frozenset(families)


def _arches(raw: Any) -> frozenset[Arch]:
    values = [raw] if isinstance(raw, str) else list(raw or [])
    arches = {Arch(str(item).lower()) for item in values}
    if not arches:
        raise ValueError("arches must name at least one architecture")
    return frozenset(arches)


def _gui_support(raw: Any, guest_os: frozenset[OSFamily]) -> GuiSupport:
    if raw is None:
        return "rdp" if OSFamily.WINDOWS in guest_os and OSFamily.LINUX not in guest_os else "vnc"
    value = str(raw).lower()
    if value not in ("none", "vnc", "rdp"):
        raise ValueError(f"gui must be one of none, vnc, rdp; got {raw!r}")
    return value  # type: ignore[return-value]


__all__ = ["LibvirtProvider", "domain_xml"]
