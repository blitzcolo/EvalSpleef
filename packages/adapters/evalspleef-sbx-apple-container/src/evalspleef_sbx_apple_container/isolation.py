"""Isolation profile (FR-SBX-11) and Linux probe set for apple/container.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` in this directory carries the same
content for the isolation audit and is checked against this definition by the
tests. ``probes/linux.sh`` is the Linux preflight probe set (PRD appendix D.2,
TECH-DESIGN section 7.7): the control plane feeds it to the sandbox over stdin
and records the per-check results.

Facts below were checked on macOS 27.2 with ``container`` 1.6.1 (guest kernel
Linux 6.18.35 aarch64): each container is a Virtualization.framework VM with its
own kernel (L3). The I1-I9 probes are ``$ <command>`` lines that hold in every
network mode and were run in real containers.

I7 is waived for pids and disk only: the CLI enforces cpu and memory, but has
no pids limit and no disk quota for a container.
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
            how="every trial boots a fresh lightweight VM (its own guest kernel) from an "
            "immutable OCI image and the container is deleted after the trial; the lease "
            "id is a label",
            probe='$ test "$(cut -d. -f1 /proc/uptime)" -lt 900',
        ),
        "I2": InvariantEntry(
            how="containers start with --read-only (the root is a read-only ext4 block "
            "device); the only writable paths are the tmpfs mounts /tmp, /workspace and "
            "/home/<run_as_user>, plus the paths a spec declares in writable_paths. Files move as tar streams over exec as the sandbox user; "
            "the host-side 'container cp' writes beneath --read-only and under the tmpfs "
            "mounts, so the provider never uses it",
            probe="$ ! touch /.spleef-probe 2>/dev/null",
        ),
        "I3": InvariantEntry(
            how="the VM root is per-trial and /workspace, /tmp, the agent home (and declared "
            "writable paths) are tmpfs in guest memory; no host directory is shared into the VM (no --volume, "
            "--mount or virtiofs share)",
            probe='$ awk \'$2 == "/workspace" { t = $3 } END { exit t != "tmpfs" }\' /proc/mounts',
        ),
        "I4": InvariantEntry(
            how="every container is its own VM with its own kernel network stack. "
            "NetMode.OFF runs it with '--network none': loopback only, no route, no DNS, "
            "no host. NetMode.FULL attaches the 'default' vmnet NAT network. An "
            "'--internal' network is host-only (the VM reaches every service listening on "
            "the host), so it is never used for OFF",
            probe="$ test -z \"$(ls /sys/class/net | grep -Ev '^(lo|eth0)$')\"",
        ),
        "I5": InvariantEntry(
            how="no --volume/--mount/--ssh/--publish/--publish-socket/--virtualization "
            "flags, --cap-drop ALL (effective capabilities are empty, root included) and a "
            "read-only root on every container; no container-runtime socket exists in the guest",
            probe="$ ! grep -q ' virtiofs ' /proc/mounts && grep -Eq '^CapEff:[[:space:]]*0+$' "
            "/proc/self/status && test ! -S /var/run/docker.sock && test ! -S /run/docker.sock",
        ),
        "I6": InvariantEntry(
            how="the init process and every exec run as the non-root run_as_user (--user); "
            "the account must exist in the image (the CLI refuses an unknown user at start) "
            "and task images must not ship sudo",
            probe='$ test "$(id -u)" != 0 && ! sudo -n true 2>/dev/null',
        ),
        "I7": InvariantEntry(
            how="'--cpus' sets the guest cgroup cpu.max to cpus x 100000 (the VM gets one "
            "extra vCPU of overhead, so nproc reports cpus + 1); '--memory' sets memory.max "
            "to exactly memory_mb (the VM's RAM is slightly larger). pids.max stays 'max' "
            "and there is no disk quota",
            probe='$ test "$(cat /sys/fs/cgroup/memory.max)" != max && '
            "test \"$(cut -d' ' -f1 /sys/fs/cgroup/cpu.max)\" != max",
            waived=True,
            reason="the container CLI enforces cpu and memory but has no pids limit and no "
            "disk quota for a container",
            compensating="each sandbox is its own VM, so pid exhaustion stays inside its own "
            "guest kernel; every writable path is tmpfs charged to the memory cgroup (a write "
            "past memory.max is OOM-killed), so writable storage is bounded by memory_mb; the "
            "TTL sweep bounds the container lifetime",
            approved_by="evaluation lead",
            expires="2027-06-30",
        ),
        "I8": InvariantEntry(
            how="the provider injects no evaluation identity: only spec.env (task environment) "
            "plus proxy settings reach an exec. The container name is also the guest "
            "hostname, so name_prefix must stay neutral (default 'c-')",
            probe="$ ! env | grep -Eq '^[A-Z_]*(TASK|BATCH|MODEL|EVAL)[A-Z_]*='",
        ),
        "I9": InvariantEntry(
            how="OCI images are referenced by digest in TemplateRef and run as "
            "ref@sha256:... (the CLI pulls and verifies that digest); the only credential "
            "allowed in a sandbox is the per-trial gateway key, which the provider never injects",
            probe="$ ! env | grep -Eiq '^[a-z_]*(api_key|secret|token|password)[a-z_]*='",
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
            "which the provider does not touch; upload(read_only=True) unpacks a tree as "
            "root without group/other write bits",
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
            "internal (host-only) network with no route off the host and is pointed at the "
            "proxy, which logs URLs. A host-only network also exposes every other service "
            "listening on the host's address there, so the proxy host must expose nothing "
            "else on it. Open networking stays platform-logged",
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
