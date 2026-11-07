"""Isolation profile (FR-SBX-11) and guest probe sets for the libvirt provider.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` in this directory carries the same
content for the isolation audit and is checked against this definition by the
tests.

``probes/linux.sh`` and ``probes/windows.ps1`` are the guest-side preflight
probe sets (PRD appendix D.2, TECH-DESIGN section 7.7). The control plane feeds
them to the guest over exec stdin and records the per-check results.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation, OSFamily
from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PROBE_PACKAGE = "evalspleef_sbx_libvirt"
_PROBE_RESOURCES = {
    OSFamily.LINUX: "probes/linux.sh",
    OSFamily.WINDOWS: "probes/windows.ps1",
}

_WAIVER_EXPIRES = "2027-03-01"
_WAIVER_APPROVER = "evaluation lead"


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial gets a fresh qcow2 overlay over the immutable golden image "
            "and a newly defined domain; the domain is undefined and the overlay deleted "
            "after the trial, and the lease id is recorded as a domain label",
            probe="list(labels) returns the domain while live and nothing after destroy",
        ),
        "I2": InvariantEntry(
            how="waived: the guest root disk is writable for the duration of the trial",
            probe="mount writes are discarded with the per-trial overlay",
            waived=True,
            reason="the guest kernel and toolchain expect a writable root disk; the "
            "root disk is a copy-on-write overlay that is destroyed with the domain",
            compensating="key-path hash comparison plus canary token scan before and "
            "after the trial; the overlay is never reused",
            approved_by=_WAIVER_APPROVER,
            expires=_WAIVER_EXPIRES,
        ),
        "I3": InvariantEntry(
            how="each trial writes only into its own overlay and its own /workspace; "
            "the golden image is opened read-only as the backing file and nothing is "
            "shared between domains",
            probe="a marker written by one trial is absent in the next trial's guest",
        ),
        "I4": InvariantEntry(
            how="NetMode.OFF attaches the domain to an isolated libvirt network with no "
            "forwarding, plus an nwfilter that drops all egress; allowlisted traffic "
            "uses the proxy network and the matching nwfilter",
            probe="curl --max-time 3 https://example.com exits non-zero in an off sandbox",
        ),
        "I5": InvariantEntry(
            how="no host directories are mounted into the guest and no host devices or "
            "container sockets are passed through; only the overlay disk and the "
            "virtual NIC are attached",
            probe="the guest mount table shows no host path and no container socket",
        ),
        "I6": InvariantEntry(
            how="commands run as the non-root run_as_user over SSH; the template user "
            "has no sudo rule",
            probe="id -u is not 0 and sudo -n true fails",
        ),
        "I7": InvariantEntry(
            how="vcpu and memory are set from the requested Resources in the domain XML "
            "and enforced by the hypervisor",
            probe="nproc and /proc/meminfo stay within the requested cpu and memory",
        ),
        "I8": InvariantEntry(
            how="the provider injects no evaluation identity: the domain carries labels "
            "only in host-side metadata and the guest hostname is the template's neutral "
            "name; runner-side env -i whitelisting sits on top",
            probe="env contains no TASK/BATCH/MODEL/EVAL-style names",
        ),
        "I9": InvariantEntry(
            how="templates are referenced by sha256 digest in TemplateRef and the backing "
            "file is resolved locally; long-lived credentials never enter the domain XML "
            "or the guest, only the per-trial gateway key is allowed",
            probe="qemu-img info reports the recorded backing format; the guest env scan "
            "shows no long-lived credentials",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to "
            "their own domains and leases; the provider never mixes them",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download walks the remote tree and rejects absolute or escaping "
            "symlinks, special files and setuid/setgid bits, and enforces size caps",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter agent-side templates; hash locks live in the "
            "task pack, which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="a fresh overlay per trial means no state carries over; canary token "
            "placement is a control-plane concern recorded per trial",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="domains are destroyed at trial end and never returned to a pool; "
            "orphan domains are reclaimed by label through reap() and the TTL label",
            probe="list(labels) is empty after destroy; spleef.expires_at bounds the "
            "domain lifetime",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped domain like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="docs_whitelist/package_mirror are only offered when the provider is "
            "configured with an egress proxy and the policy carries via_proxy; the "
            "nwfilter permits only the proxy address, and the proxy logs every request",
            probe="the egress proxy's url.jsonl has one line per request; direct egress "
            "bypass is not offered",
        ),
    }


def isolation_profile() -> IsolationProfile:
    """The I1-I16 profile for this adapter."""
    return IsolationProfile(
        provider="libvirt", declared_isolation=Isolation.L3, invariants=_entries()
    )


@lru_cache(maxsize=len(_PROBE_RESOURCES))
def probe_script(os_family: OSFamily) -> str:
    """The guest preflight probe script for ``os_family``, delivered over exec stdin."""
    resource_name = _PROBE_RESOURCES.get(os_family)
    if resource_name is None:
        raise CapabilityNotSupported(f"no isolation probe for OS family {os_family.value!r}")
    resource = resources.files(_PROBE_PACKAGE).joinpath(resource_name)
    return resource.read_text(encoding="utf-8")
