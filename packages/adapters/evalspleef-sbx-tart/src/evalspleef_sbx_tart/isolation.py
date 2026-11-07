"""Isolation profile (FR-SBX-11) and guest probe set for the Tart provider.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` beside it carries the same content for
the isolation audit and is checked against this definition by the tests.
``probes/macos.sh`` and ``probes/linux.sh`` are the per-OS preflight probe sets
(TECH-DESIGN section 7.7); they are fed to the guest over ``exec`` stdin.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation, OSFamily
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import Sandbox

_PROBE_PACKAGE = "evalspleef_sbx_tart"
_PROBE_RESOURCES = {
    OSFamily.MACOS: "probes/macos.sh",
    OSFamily.LINUX: "probes/linux.sh",
}


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial clones the golden image by digest into its own VM and the "
            "VM is stopped and deleted after the trial; the lease id is recorded in the "
            "provider registry",
            probe="list(labels) returns exactly one instance per lease while live",
        ),
        "I2": InvariantEntry(
            how="waived: the guest root disk is writable by design (a VM is not a container "
            "rootfs); every trial still starts from a fresh clone of the immutable image",
            probe="the disk is destroyed with the VM at trial end",
            waived=True,
            reason="macOS and Linux guests need a writable root filesystem; a read-only "
            "root disk is not available through Virtualization.framework",
            compensating="fresh APFS clone per trial, image digest recorded, cross-trial "
            "canary scan, and the VM is deleted (not reused) after the trial",
            approved_by="platform-evaluation-lead",
            expires="2027-03-01",
        ),
        "I3": InvariantEntry(
            how="each VM is a copy-on-write clone of the golden image; /workspace, /tmp and "
            "the agent home start empty in every clone and no host directory is shared into "
            "the guest",
            probe="a marker written in one trial is absent in the next trial's VM",
        ),
        "I4": InvariantEntry(
            how="network mode off runs the VM with --net-softnet-block 0.0.0.0/0, so no "
            "packet leaves the guest",
            probe="curl --max-time 3 https://example.com exits non-zero in an off-network VM",
        ),
        "I5": InvariantEntry(
            how="tart run is started without any host directory sharing; there is no host "
            "socket or device passed into the guest",
            probe="mount shows no virtiofs/9p/smbfs host share and no host socket",
        ),
        "I6": InvariantEntry(
            how="commands run through sudo -u agent -i from the management account; the "
            "agent account is not in the sudoers file, so the agent cannot regain root",
            probe="id -u is not 0 and sudo -n true fails",
        ),
        "I7": InvariantEntry(
            how="cpu and memory are fixed with tart set --cpu and --memory before run; the "
            "guest cannot grow beyond the VM configuration",
            probe="sysctl -n hw.ncpu and hw.memsize match the requested resources",
        ),
        "I8": InvariantEntry(
            how="the golden image gets a neutral ComputerName; the provider injects no task, "
            "batch or model identity into the guest; the runner starts the agent with a "
            "whitelisted environment",
            probe="scutil --get ComputerName contains no evaluation marker; env scan is clean",
        ),
        "I9": InvariantEntry(
            how="the VM is cloned from a template referenced by digest; the only credential "
            "allowed in a sandbox is the per-trial gateway key, which the provider never adds",
            probe="describe() reports the template digest; env scan shows no long-lived keys",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to their "
            "own VMs and leases; the provider never mixes them",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download sanitises the artifact stream: symlinks that resolve outside the "
            "export root, special files and setuid bits are rejected, sizes are capped",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter the agent-side golden image; hash locks live in "
            "the task pack, which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="the provider clones a fresh VM per trial so no state can carry over; "
            "canary token placement is a runner/control-plane concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="VMs are stopped and deleted at trial end and never returned to a pool; "
            "orphans are reclaimed by label through reap() and the TTL sweep, and every "
            "registry entry carries spleef.expires_at",
            probe="list(labels) is empty after destroy; expired entries are reaped",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped VM like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="allowlisted modes run softnet with only the egress proxy /32 (or the "
            "task CIDRs) permitted, so all allowlisted traffic flows through the logging "
            "egress proxy; hostnames are only offered with via_proxy",
            probe="the egress proxy's url.jsonl has one line per request; direct egress "
            "bypass is not offered",
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L3) -> IsolationProfile:
    """The I1-I16 profile for this adapter, with ``declared`` as the isolation level."""
    return IsolationProfile(provider="tart", declared_isolation=declared, invariants=_entries())


@lru_cache(maxsize=2)
def probe_script(os_family: OSFamily) -> str:
    """The preflight probe script for ``os_family`` (macOS or Linux guest)."""
    resource = _PROBE_RESOURCES.get(os_family)
    if resource is None:
        raise ValueError(f"no probe set for OS family {os_family.value!r}")
    return resources.files(_PROBE_PACKAGE).joinpath(resource).read_text(encoding="utf-8")


async def run_isolation_probes(
    sandbox: Sandbox,
    os_family: OSFamily,
    *,
    timeout_s: float = 120.0,
) -> tuple[int, bytes]:
    """Run the shipped probe set through ``exec`` (stdin) and return (exit code, stdout)."""
    script = probe_script(os_family)
    result = await sandbox.exec(
        ["/bin/sh", "-s"],
        stdin=script.encode("utf-8"),
        timeout_s=timeout_s,
    )
    return result.exit_code, result.stdout
