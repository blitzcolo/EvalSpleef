"""Isolation profile (FR-SBX-11) and guest probe sets for the Hyper-V provider.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` beside it carries the same content for
the isolation audit and is checked against this definition by the tests.
``probes/windows.ps1`` is the preflight probe set for Windows guests and
``probes/linux.sh`` the Linux variant (TECH-DESIGN section 7.7); the control
plane feeds them to the guest over ``exec`` stdin.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation, OSFamily
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import Sandbox

_PROBE_PACKAGE = "evalspleef_sbx_hyperv"
_PROBE_RESOURCES = {
    OSFamily.WINDOWS: "probes/windows.ps1",
    OSFamily.LINUX: "probes/linux.sh",
}


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial gets its own VM on a differencing VHD created from the "
            "golden image and the VM plus its disk are destroyed after the trial; the "
            "lease id is recorded in the provider registry",
            probe="list(labels) returns exactly one instance per lease while live",
        ),
        "I2": InvariantEntry(
            how="waived: the guest disk is writable by design (a VM is not a container "
            "rootfs); every trial still starts from a fresh differencing disk chained "
            "to the immutable golden VHDX",
            probe="the differencing disk is deleted with the VM at trial end",
            waived=True,
            reason="Windows and Linux guests need a writable system disk; a read-only "
            "root disk is not practical for Windows servicing and toolchains",
            compensating="fresh differencing VHD per trial chained to the sha256-pinned "
            "golden image, cross-trial canary scan, and the disk is deleted (not "
            "reused) when the trial ends",
            approved_by="platform-evaluation-lead",
            expires="2027-03-01",
        ),
        "I3": InvariantEntry(
            how="each VM's writes go to its own differencing disk; C:\\workspace (and "
            "/workspace on Linux guests) start empty in every trial and no host "
            "directory, shared VHDX or Enhanced Session drive is attached",
            probe="a marker written in one trial is absent in the next trial's VM",
        ),
        "I4": InvariantEntry(
            how="network mode off connects the VM to a Private vSwitch, which has no "
            "external uplink at all, so no packet can leave the host",
            probe="Test-NetConnection to an external host fails in an off-network VM",
        ),
        "I5": InvariantEntry(
            how="no host path is mounted into the guest: no shared VHDX, no SMB share, "
            "no Enhanced Session redirected drive, no device passthrough; PowerShell "
            "Direct is host-administator-only and the guest exposes no endpoint for it",
            probe="Get-SmbShare shows no host share and the probe set reports clean",
        ),
        "I6": InvariantEntry(
            how="the golden image creates a non-administrator agent account and the "
            "OpenSSH Server is configured for that account only; the account is not in "
            "the Administrators group",
            probe="whoami /groups has no Administrators entry; sudo-equivalent checks "
            "are covered by the Windows probe set",
        ),
        "I7": InvariantEntry(
            how="Set-VMProcessor -Count and Set-VMMemory -StartupBytes fix the CPU and "
            "memory configuration before Start-VM; the guest cannot grow beyond the VM "
            "configuration",
            probe="the guest sees the requested vCPU count and memory size",
        ),
        "I8": InvariantEntry(
            how="the golden image carries a neutral computer name; the provider injects "
            "no task, batch or model identity into the guest and the runner starts the "
            "agent with a whitelisted environment",
            probe="Env: contains no credential or evaluation identifier (Windows probe "
            "set env check)",
        ),
        "I9": InvariantEntry(
            how="the golden VHDX is referenced by sha256 in TemplateRef and the builder "
            "records it in the template registry; the only credential allowed in a "
            "sandbox is the per-trial gateway key, which the provider never adds",
            probe="describe() reports the template digest; the Env: scan shows no "
            "long-lived credentials",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to "
            "their own VMs and leases; the provider never mixes them",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download sanitises the artifact stream: SFTP refuses symlinks that "
            "resolve outside the workspace, and archive members that escape the export "
            "root, special files and setuid bits are rejected, sizes are capped",
            probe="downloading a symlink pointing at C:\\Windows\\win.ini raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter the agent-side guest; hash locks live in the "
            "task pack, which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="the provider creates a fresh VM per trial so no state can carry over; "
            "canary token placement is a runner/control-plane concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="VMs are stopped and removed at trial end and never returned to a pool; "
            "orphans are reclaimed by label through reap() and the TTL sweep, and every "
            "registry entry carries an expires_at lifetime guard",
            probe="list(labels) is empty after destroy; expired entries are reaped",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped VM like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="allowlisted modes connect an Internal vSwitch and add "
            "Add-VMNetworkAdapterAcl rules that permit only the egress proxy address "
            "(or the task CIDRs), so all allowlisted traffic flows through the logging "
            "egress proxy; hostname allowlists are only offered with via_proxy",
            probe="the egress proxy's url.jsonl has one line per request; direct "
            "egress bypass is not offered",
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L3) -> IsolationProfile:
    """The I1-I16 profile for this adapter, with ``declared`` as the isolation level."""
    return IsolationProfile(provider="hyperv", declared_isolation=declared, invariants=_entries())


@lru_cache(maxsize=2)
def probe_script(os_family: OSFamily) -> str:
    """The preflight probe script for ``os_family`` (Windows or Linux guest)."""
    resource = _PROBE_RESOURCES.get(os_family)
    if resource is None:
        raise ValueError(f"no probe set for OS family {os_family.value!r}")
    return resources.files(_PROBE_PACKAGE).joinpath(resource).read_text(encoding="utf-8")


async def run_isolation_probes(
    sandbox: Sandbox,
    os_family: OSFamily,
    *,
    timeout_s: float = 300.0,
) -> tuple[int, bytes]:
    """Run the shipped probe set through ``exec`` and return (exit code, stdout).

    The Linux probe is fed over ``exec`` stdin (``/bin/sh -s``); the Windows
    probe runs as one PowerShell command because that is the guest's default
    shell and keeps multi-line script delivery quoting-free.
    """
    script = probe_script(os_family)
    if os_family is OSFamily.WINDOWS:
        result = await sandbox.exec(script, timeout_s=timeout_s)
    else:
        result = await sandbox.exec(
            ["/bin/sh", "-s"],
            stdin=script.encode("utf-8"),
            timeout_s=timeout_s,
        )
    return result.exit_code, result.stdout
