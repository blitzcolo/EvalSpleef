"""Isolation profile (FR-SBX-11) and Linux probe set for the agent-sandbox provider.

``isolation_profile.yaml`` carries the same content for the isolation audit;
the tests keep the two in sync. ``probes/linux.sh`` is fed to a sandbox through
``Sandbox.exec`` by the control plane (TECH-DESIGN section 7.7).
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PROBE_PACKAGE = "evalspleef_sbx_k8s_agent_sandbox"
_LINUX_PROBE_RESOURCE = "probes/linux.sh"


def _entries() -> dict[str, InvariantEntry]:
    return {
        "I1": InvariantEntry(
            how="every trial gets a fresh Sandbox custom resource named spleef-<nonce>; "
            "the SPI labels including the lease id are copied onto the object, and the "
            "object is deleted after the trial",
            probe="list(labels) returns exactly one Sandbox per lease while live",
        ),
        "I2": InvariantEntry(
            how="the pod container runs with readOnlyRootFilesystem; only the emptyDir "
            "volumes mounted at /workspace and /tmp are writable",
            probe="findmnt -no OPTIONS / starts with ro and touch /.__probe fails",
        ),
        "I3": InvariantEntry(
            how="/workspace and /tmp are per-pod emptyDir volumes; no hostPath or "
            "persistent volume is ever mounted into an agent sandbox",
            probe="a marker written by one sandbox is absent in the next sandbox",
        ),
        "I4": InvariantEntry(
            how="NetMode.OFF creates a CiliumNetworkPolicy with an empty egress list, "
            "which Cilium applies as default-deny egress; full access is a deployment "
            "decision logged outside the sandbox",
            probe="curl --max-time 3 https://example.com exits non-zero in an off sandbox",
        ),
        "I5": InvariantEntry(
            how="automountServiceAccountToken is false, no hostPath volume exists, "
            "capabilities drop ALL, allowPrivilegeEscalation is false and the pod uses "
            "the RuntimeDefault seccomp profile",
            probe="the mount table shows no host path and no service-account token",
        ),
        "I6": InvariantEntry(
            how="the pod security context sets runAsNonRoot (and runAsUser when the "
            "spec names a numeric account); task images must not ship passwordless sudo",
            probe="id -u is not 0 and sudo -n true fails",
        ),
        "I7": InvariantEntry(
            how="cpu and memory requests equal their limits, so the kubelet enforces "
            "them through cgroups; ephemeral storage is requested and limited for the "
            "workspace volume",
            probe="cpu.max and memory.max match the requested resources",
        ),
        "I8": InvariantEntry(
            how="the pod hostname is the neutral constant 'sandbox'; the provider adds "
            "no task, batch or model variables to the environment, only the SPI env",
            probe="env contains no TASK/BATCH/MODEL/EVAL-style names",
        ),
        "I9": InvariantEntry(
            how="the image is referenced as ref@sha256:... when the template digest is "
            "real; the only credential allowed in the sandbox is the per-trial gateway "
            "key, which the provider never injects",
            probe="the pod image field carries a sha256 digest",
        ),
        "I10": InvariantEntry(
            how="verifier and judge roles are separate SandboxSpec roles scheduled to "
            "their own Sandbox objects and leases",
            probe="verifier_lease_id != agent_lease_id for a trial",
        ),
        "I11": InvariantEntry(
            how="download extracts the tar stream with sanitisation: escaping symlinks, "
            "special files, setuid bits and oversized members are rejected",
            probe="downloading a symlink pointing at /etc/passwd raises TransferError",
        ),
        "I12": InvariantEntry(
            how="hidden tests never enter agent images; hash locks live in the task pack, "
            "which the provider does not touch",
            probe="tests.sha256 manifest matches the verifier-side files",
        ),
        "I13": InvariantEntry(
            how="every trial creates a new Sandbox and pod with emptyDir storage, so no "
            "state carries over; canary placement is a runner concern",
            probe="cross-trial canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="warm-pool instances are only claimed through a SandboxClaim and are "
            "never returned to the pool: destroy deletes the claim and its sandbox, and "
            "reap deletes orphans by label and lifetime",
            probe="destroy is idempotent and list(labels) is empty afterwards",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in their own trial-scoped Sandbox like any other role",
            probe="judge session is unique per trial",
        ),
        "I16": InvariantEntry(
            how="docs_whitelist/package_mirror generate Cilium toFQDNs rules plus a DNS "
            "rule; off is default-deny; full networking stays platform-logged",
            probe="a non-allowlisted hostname is unreachable and the proxy/CNI logs one "
            "record per request",
        ),
    }


def isolation_profile(declared: Isolation = Isolation.L2) -> IsolationProfile:
    """The I1-I16 profile for this adapter with ``declared`` as the isolation level."""
    return IsolationProfile(
        provider="k8s-agent-sandbox",
        declared_isolation=declared,
        invariants=_entries(),
    )


@lru_cache(maxsize=1)
def linux_probe_script() -> str:
    """The Linux isolation probe script, delivered to the sandbox over exec."""
    resource = resources.files(_PROBE_PACKAGE).joinpath(_LINUX_PROBE_RESOURCE)
    return resource.read_text(encoding="utf-8")
