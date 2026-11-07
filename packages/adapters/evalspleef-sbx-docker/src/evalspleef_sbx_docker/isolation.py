"""Isolation profile (FR-SBX-11) and Linux probe set for the Docker provider.

The profile is defined here in code because that is what ``isolation_profile()``
must return; ``isolation_profile.yaml`` in this directory carries the same
content for the isolation audit and is checked against this definition by the
tests. ``probes/linux.sh`` is the preflight probe set for Linux containers
(PRD appendix D.2, TECH-DESIGN section 7.7): the control plane feeds it to the
sandbox over stdin and records the per-check results.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PROBE_PACKAGE = "evalspleef_sbx_docker"
_LINUX_PROBE_RESOURCE = "probes/linux.sh"


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial gets a fresh container from an immutable image and the "
            "container is destroyed after the trial; the lease id is recorded as a label",
            probe="list(labels) returns exactly one instance per lease while live",
        ),
        "I2": InvariantEntry(
            how="containers are created with a read-only root filesystem; the only "
            "writable paths are tmpfs mounts (/tmp, the agent home, /workspace)",
            probe="findmnt -no OPTIONS / starts with ro and touch /.__probe fails",
        ),
        "I3": InvariantEntry(
            how="/tmp, the agent home and /workspace are per-trial tmpfs mounts; no "
            "host directory is ever bind-mounted into the container",
            probe="findmnt -no FSTYPE /tmp is tmpfs; a marker written by one trial "
            "is absent in the next trial's container",
        ),
        "I4": InvariantEntry(
            how="every container gets a private network namespace; NetMode.OFF maps "
            "to the Docker 'none' network so there is no external egress",
            probe="curl --max-time 3 https://example.com exits non-zero in an off-network sandbox",
        ),
        "I5": InvariantEntry(
            how="no host bind mounts, no Docker socket, --cap-drop ALL and "
            "no-new-privileges on every container",
            probe="the mount table shows no writable host path and test ! -S /var/run/docker.sock",
        ),
        "I6": InvariantEntry(
            how="containers run as the non-root run_as_user; task images must not "
            "ship sudo (the conformance test image installs none)",
            probe="id -u is not 0 and sudo -n true fails",
        ),
        "I7": InvariantEntry(
            how="cpu (nano_cpus), memory (memory + equal memory_swap) and pids "
            "(pids_limit) are enforced through cgroup v2",
            probe="cat /sys/fs/cgroup/{cpu.max,memory.max,pids.max} matches the "
            "requested resources",
        ),
        "I8": InvariantEntry(
            how="the provider injects no evaluation identity: no task, batch or model "
            "variables enter the container environment and the hostname is the "
            "neutral container id",
            probe="env contains no TASK/BATCH/MODEL/EVAL-style names (runner-side "
            "env -i whitelist on top)",
        ),
        "I9": InvariantEntry(
            how="images are referenced by digest in TemplateRef and pulled by digest "
            "when not present locally; the only credential allowed in a sandbox is "
            "the per-trial gateway key, which the provider never injects",
            probe="image ref resolves to a sha256:... id; env scan shows no long-lived credentials",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to "
            "their own containers and leases; the provider never mixes them",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download sanitises the artifact stream: symlinks that resolve "
            "outside the export root, special files and setuid bits are rejected, "
            "sizes are capped",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter agent-side images; hash locks live in the "
            "task pack, which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="the provider creates a fresh container per trial so no state can "
            "carry over; canary token placement is a runner/control-plane concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="containers are destroyed at trial end and never returned to a pool; "
            "orphans are reclaimed by label through reap() and the TTL sweep",
            probe="list(labels) is empty after destroy; spleef.expires_at bounds "
            "the container lifetime",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped container like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="docs_whitelist/package_mirror are only offered when the provider "
            "config names an egress network and the policy carries via_proxy, so all "
            "allowlisted traffic flows through the logging egress proxy; full "
            "networking stays platform-logged",
            probe="the egress proxy's url.jsonl has one line per request; direct "
            "egress bypass is not offered",
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L1) -> IsolationProfile:
    """The I1-I16 profile for this adapter, with ``declared`` as the isolation level."""
    return IsolationProfile(provider="docker", declared_isolation=declared, invariants=_entries())


@lru_cache(maxsize=1)
def linux_probe_script() -> str:
    """The Linux preflight probe script (PRD appendix D.2), delivered over exec stdin."""
    resource = resources.files(_PROBE_PACKAGE).joinpath(_LINUX_PROBE_RESOURCE)
    return resource.read_text(encoding="utf-8")
