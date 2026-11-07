"""Isolation profile (FR-SBX-11) and Windows probe set for the Windows Sandbox provider.

Windows Sandbox gives a disposable Windows guest but very few hardening knobs:
one mapped folder, networking on or off, a fixed standard user. The profile
declares L3 and records every invariant the adapter cannot enforce as a waiver
with a reason, a compensating control and an expiry date. The ``I`` ids and the
waiver fields follow ``evalspleef.spi.isolation``; ``isolation_profile.yaml``
carries the same content for the isolation audit and the tests keep both in
sync.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PROBE_PACKAGE = "evalspleef_sbx_windows_sandbox"
_WINDOWS_PROBE_RESOURCE = "probes/windows.ps1"
_EXPIRES = "2027-06-30"
_APPROVED_BY = "evalspleef-platform"


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial starts a fresh Windows Sandbox instance from the host "
            "image and stops it after the trial; the lease id is recorded in the "
            "instance metadata labels",
            probe="list(labels) returns one instance per lease while live and none after destroy",
        ),
        "I2": InvariantEntry(
            how="the guest system image is per-instance and discarded on stop; "
            "Windows Sandbox exposes no read-only root filesystem",
            waived=True,
            reason="Windows Sandbox has no read-only system drive and no hardening knobs "
            "beyond the mapped folder and networking",
            compensating="one fresh instance per trial, destroyed afterwards; no state "
            "is carried between trials",
            approved_by=_APPROVED_BY,
            expires=_EXPIRES,
        ),
        "I3": InvariantEntry(
            how="the only host path visible in the guest is the per-trial mapped "
            "folder holding /workspace",
            waived=True,
            reason="a mapped folder is a host directory by construction and there is no "
            "tmpfs or volume layer inside Windows Sandbox",
            compensating="the mapped folder is created per trial under the provider "
            "state directory and removed on destroy; transfer paths are checked "
            "against /workspace",
            approved_by=_APPROVED_BY,
            expires=_EXPIRES,
        ),
        "I4": InvariantEntry(
            how="NetMode.OFF writes <Networking>Disable</Networking> into the config "
            "XML; NetMode.FULL enables it and nothing in between",
            probe="Test-NetConnection example.com:443 fails in an off-network sandbox "
            "and the firewall profiles block outbound by default",
        ),
        "I5": InvariantEntry(
            how="the provider maps exactly one host folder; no other mount or host "
            "socket is exposed",
            waived=True,
            reason="the mapped workspace is a writable host path and cannot be avoided "
            "with the wsb CLI",
            compensating="only the per-trial workspace is mapped, the workspace holds "
            "no credentials and the mapping disappears with the instance",
            approved_by=_APPROVED_BY,
            expires=_EXPIRES,
        ),
        "I6": InvariantEntry(
            how="wsb runs commands as the built-in WDAGUtilityAccount, which is not an "
            "administrator",
            probe="whoami /groups does not contain S-1-5-32-544 (probes/windows.ps1)",
        ),
        "I7": InvariantEntry(
            how="Windows Sandbox has no cpu, disk or pids controls; MemoryInMB is "
            "written to the config but not verified",
            waived=True,
            reason="the wsb CLI cannot enforce cpu, disk or pids limits and memory "
            "enforcement is unverified on a real host",
            compensating="one instance per host and a bounded max_lifetime_s keep a "
            "runaway guest from starving the host",
            approved_by=_APPROVED_BY,
            expires=_EXPIRES,
        ),
        "I8": InvariantEntry(
            how="the provider passes no evaluation identity into the guest: the config "
            "XML carries no environment and the command line only carries the env the "
            "caller asks for",
            probe="Get-ChildItem Env: contains no task/batch/model/canary names "
            "(probes/windows.ps1)",
        ),
        "I9": InvariantEntry(
            how="the template digest is recorded in the instance metadata; the only "
            "credential meant for a sandbox is the per-trial gateway key, which the "
            "provider never writes into the mapped folder",
            probe="env and probe scan show no long-lived credentials",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpecs; one instance per "
            "host means roles can never share a guest",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download rejects paths and symlinks resolving outside the mapped "
            "workspace, rejects special files and honours max_bytes",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter the mapped folder through the provider; hash "
            "locks live in the task pack, which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="a fresh instance per trial means no state can carry over; canary "
            "placement is a runner/control-plane concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="instances are stopped at trial end and never returned to a pool; "
            "orphans are reclaimed by label through reap() and spleef.expires_at "
            "bounds every instance lifetime",
            probe="list(labels) is empty after destroy; spleef.expires_at bounds the "
            "instance lifetime",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped instance like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="the provider offers no allowlist or egress proxy: networking is off "
            "or full, and full traffic is not logged",
            waived=True,
            reason="wsb has no firewall or proxy hook inside the guest, so an allowlist "
            "cannot be enforced or audited",
            compensating="dev_only=True keeps the adapter out of every formal batch "
            "route, and allowlist policies are rejected at create",
            approved_by=_APPROVED_BY,
            expires=_EXPIRES,
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L3) -> IsolationProfile:
    """The I1-I16 profile for this adapter."""
    return IsolationProfile(
        provider="windows-sandbox",
        declared_isolation=declared,
        verified_isolation=None,
        invariants=_entries(),
    )


@lru_cache(maxsize=1)
def windows_probe_script() -> str:
    """The Windows guest preflight probe script (PRD appendix D.2), run over ``exec``."""
    resource = resources.files(_PROBE_PACKAGE).joinpath(_WINDOWS_PROBE_RESOURCE)
    return resource.read_text(encoding="utf-8")
