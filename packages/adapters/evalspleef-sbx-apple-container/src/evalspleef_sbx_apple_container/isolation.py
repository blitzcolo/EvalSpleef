"""Isolation profile (FR-SBX-11) and Linux probe set for apple/container.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` in this directory carries the same
content for the isolation audit and is checked against this definition by the
tests. ``probes/linux.sh`` is the Linux preflight probe set (PRD appendix D.2,
TECH-DESIGN section 7.7): the control plane feeds it to the sandbox over stdin
and records the per-check results.

I7 is waived for pids and disk only: the CLI enforces cpu and memory, but has
no per-VM pids or disk quota.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PROBE_PACKAGE = "evalspleef_sbx_apple_container"
_LINUX_PROBE_RESOURCE = "probes/linux.sh"


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial gets a fresh lightweight VM from an immutable OCI image and "
            "the container is removed after the trial; the lease id is a label",
            probe="list(labels) returns exactly one instance per lease while live",
        ),
        "I2": InvariantEntry(
            how="containers start with --read-only; the only writable paths are the tmpfs "
            "mounts /tmp, /workspace and the agent home",
            probe="findmnt -no OPTIONS / starts with ro and touch /.__probe fails",
        ),
        "I3": InvariantEntry(
            how="the VM disk is per-trial and /workspace, /tmp and the agent home are tmpfs; "
            "no host directory is ever mounted into the container",
            probe="findmnt -no FSTYPE /workspace is tmpfs; a marker from one trial is absent "
            "in the next",
        ),
        "I4": InvariantEntry(
            how="every container runs in its own VM and network namespace; NetMode.OFF "
            "attaches it to an internal network created with 'network create --internal'",
            probe="curl --max-time 3 https://example.com exits non-zero in an off sandbox",
        ),
        "I5": InvariantEntry(
            how="no --volume/--mount/--ssh flags, no published sockets, --cap-drop ALL and a "
            "read-only root filesystem on every container",
            probe="the mount table shows no writable host path and no container socket exists",
        ),
        "I6": InvariantEntry(
            how="the init process and every exec run as the non-root run_as_user; task images "
            "must not ship sudo",
            probe="id -u is not 0 and sudo -n true fails",
        ),
        "I7": InvariantEntry(
            how="cpu and memory are enforced with '--cpus' and '--memory' (and visible in the "
            "guest cgroup); the CLI has no pids or disk quota for a container",
            probe="cat /sys/fs/cgroup/cpu.max and memory.max match the requested resources",
            waived=True,
            reason="the container CLI sets cpu and memory limits but exposes no per-VM pids "
            "or disk quota",
            compensating="C-ISO-05 checks cpu and memory; the host keeps disk headroom and "
            "the TTL sweep bounds the container lifetime",
            approved_by="evaluation lead",
            expires="2027-06-30",
        ),
        "I8": InvariantEntry(
            how="the provider injects no evaluation identity: container names are neutral and "
            "only spec.env (task environment) plus proxy settings reach an exec",
            probe="env contains no TASK/BATCH/MODEL/EVAL-style names (runner-side env -i "
            "whitelist on top)",
        ),
        "I9": InvariantEntry(
            how="OCI images are referenced by digest in TemplateRef and run as "
            "ref@sha256:...; the only credential allowed in a sandbox is the per-trial "
            "gateway key, which the provider never injects",
            probe="the image reference carries a sha256 digest; env scan shows no long-lived "
            "credentials",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to their "
            "own containers and leases; the provider never mixes them",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download re-validates the copied tree: absolute or escaping symlinks and "
            "special files are rejected, setuid/setgid bits are cleared, sizes are capped",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter agent-side images; hash locks live in the task pack, "
            "which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="the provider creates a fresh container per trial so no state can carry over; "
            "canary token placement is a runner/control-plane concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="containers are removed at trial end and never returned to a pool; orphans are "
            "reclaimed by label through reap() and the TTL sweep, and every container carries "
            "spleef.created_at / spleef.expires_at",
            probe="list(labels) is empty after destroy; spleef.expires_at bounds the lifetime",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped container like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="docs_whitelist/package_mirror are only offered when the provider config names "
            "an egress proxy and the policy carries via_proxy; the container then sits on an "
            "internal network and reaches only the proxy, which logs URLs. Open networking "
            "stays platform-logged",
            probe="the egress proxy's url.jsonl has one line per request; direct egress "
            "bypass is not offered",
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L3) -> IsolationProfile:
    """The I1-I16 profile for this adapter, with ``declared`` as the isolation level."""
    return IsolationProfile(
        provider="apple-container", declared_isolation=declared, invariants=_entries()
    )


@lru_cache(maxsize=1)
def linux_probe_script() -> str:
    """The Linux preflight probe script (PRD appendix D.2), delivered over exec stdin."""
    resource = resources.files(_PROBE_PACKAGE).joinpath(_LINUX_PROBE_RESOURCE)
    return resource.read_text(encoding="utf-8")
