"""Isolation profile (FR-SBX-11) and preflight probes for device-pool and perf-host.

Carries the I1-I16 isolation profiles for:
- DevicePoolProvider (isolation L4, physical HarmonyOS device pool)
- PerfHostProvider (isolation L4, bare-metal performance host)

Includes probes for HarmonyOS guest execution and macOS / Linux performance hosts.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

import yaml

from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile

_PACKAGE = "evalspleef_sbx_device_pool"
_PROFILE_FILENAME = "isolation_profile.yaml"


def device_pool_invariants() -> dict[str, InvariantEntry]:
    """Invariants I1-I16 for DevicePoolProvider (HarmonyOS device pool)."""
    return {
        "I1": InvariantEntry(
            how="physical device leased from inventory with exclusive lease_id; "
            "destroyed after trial and cannot be claimed by another trial concurrently",
            probe="device state in inventory transitions free -> in_use -> handover_check",
        ),
        "I2": InvariantEntry(
            how="waived",
            probe="hdc shell touch /system/.__ro_probe fails or returns read-only filesystem",
            waived=True,
            reason="physical device root filesystem is vendor-flashed firmware; "
            "build container has isolated filesystem, and return hygiene verifies baseline hash",
            compensating="baseline package diff and configuration sha256 checked at handover (I14)",
            approved_by="eval-lead",
            expires="2027-03-01",
        ),
        "I3": InvariantEntry(
            how="build container /tmp and workspace are trial-scoped; device temporary directory "
            "/data/local/tmp is wiped completely at handover",
            probe="hdc shell rm -rf /data/local/tmp/* leaves no test artifacts",
        ),
        "I4": InvariantEntry(
            how="build container has no default network egress; device communicates only "
            "via USB/TCP hdc link to the host runner",
            probe="no external internet route on build container",
        ),
        "I5": InvariantEntry(
            how="build container has no docker socket or host mounts; device access is "
            "restricted to whitelisted actions over hdc",
            probe="test ! -S /var/run/docker.sock and agent has no unrestricted shell",
        ),
        "I6": InvariantEntry(
            how="build container runs as unprivileged agent user; device actions run via "
            "standard non-root shell user over hdc",
            probe="id -u != 0 in build container",
        ),
        "I7": InvariantEntry(
            how="build container resources capped by host cgroups; device has dedicated "
            "physical hardware resources",
            probe="device hardware model and SoC verified via hdc getprop / dump",
        ),
        "I8": InvariantEntry(
            how="no evaluation identity (task, batch, model) is injected into container "
            "environment or device properties",
            probe="env scan contains no EVAL/TASK/BATCH variables",
        ),
        "I9": InvariantEntry(
            how="HAP packages and build templates are referenced by sha256 digest; "
            "signing credentials stay on host, never enter device",
            probe="manifest digests match and no private keys on device",
        ),
        "I10": InvariantEntry(
            how="verifier runs in a separate verification sandbox; hidden tests are never "
            "pushed to the device during the agent phase",
            probe="verifier_lease_id != agent_lease_id",
        ),
        "I11": InvariantEntry(
            how="evidence (screenshots, hilog) sanitized and size-capped on retrieval",
            probe="downloaded evidence passes path and size sanitization",
        ),
        "I12": InvariantEntry(
            how="hidden test cases and reference answers stay on verifier host; "
            "device holds only the application under test",
            probe="tests.sha256 checked on verifier side",
        ),
        "I13": InvariantEntry(
            how="cross-trial canary scan over retrieved logs and evidence",
            probe="canary scan reports zero critical hits",
        ),
        "I14": InvariantEntry(
            how="mandatory handover checklist: force-stop apps, uninstall non-baseline packages, "
            "clear /data/local/tmp, reboot device, and verify baseline package list + sha256 hash; "
            "failing devices are quarantined and removed from pool",
            probe="handover.json status=pass and package diff is empty",
        ),
        "I15": InvariantEntry(
            how="judge contexts run in independent isolated sessions",
            probe="judge session id is trial-scoped",
        ),
        "I16": InvariantEntry(
            how="hdc commands and host build container operations are fully logged in audit log",
            probe="audit log contains timestamped record of all hdc and exec commands",
        ),
    }


def perf_host_invariants() -> dict[str, InvariantEntry]:
    """Invariants I1-I16 for PerfHostProvider (bare-metal performance host)."""
    return {
        "I1": InvariantEntry(
            how="exclusive physical host lease with max_concurrency=1; temporary user created "
            "per trial and deleted immediately at destroy",
            probe="id <temp_user> exists during trial and is completely removed after destroy",
        ),
        "I2": InvariantEntry(
            how="waived",
            probe="test -w /Applications",
            waived=True,
            reason="bare-metal host root filesystem is persistent OS installation; "
            "trial runs strictly inside temporary user home with signed binaries only",
            compensating="only signed artifacts matching manifest sha256 are executed; "
            "temporary user and ~/Library caches deleted on destroy (I14)",
            approved_by="eval-lead",
            expires="2027-03-01",
        ),
        "I3": InvariantEntry(
            how="trial execution occurs strictly within temporary user home directory; "
            "temporary directory and DerivedData cleared after each run",
            probe="temporary user home and /tmp/spleef-* wiped after destroy",
        ),
        "I4": InvariantEntry(
            how="network disabled or restricted to documentation whitelist; no inbound traffic",
            probe="outbound internet connection fails or routes through proxy",
        ),
        "I5": InvariantEntry(
            how="agent shell is not permitted; host only runs signed artifacts from manifest",
            probe="execution of unlisted/unsigned binary is rejected with PermissionError",
        ),
        "I6": InvariantEntry(
            how="temporary user has no administrative (sudo) privileges",
            probe="sudo -n true fails for temporary user",
        ),
        "I7": InvariantEntry(
            how="exclusive physical host allocation; preflight verifies load1 < 2, LowPowerMode off, "
            "and thermal level normal before measurement",
            probe="preflight system probe checks load1, power mode, and temperature",
        ),
        "I8": InvariantEntry(
            how="no evaluation identity injected into execution environment",
            probe="env scan contains no EVAL/TASK/BATCH variables",
        ),
        "I9": InvariantEntry(
            how="artifacts must be pre-signed and listed with sha256 in manifest; "
            "no secrets or keys on host",
            probe="manifest sha256 validation enforced before launch",
        ),
        "I10": InvariantEntry(
            how="verifier executes on distinct lease or host",
            probe="verifier_lease_id != agent_lease_id",
        ),
        "I11": InvariantEntry(
            how="downloaded performance traces and logs validated and size-capped",
            probe="metrics JSON and traces validated",
        ),
        "I12": InvariantEntry(
            how="hidden benchmark suites verified via hash locks",
            probe="tests.sha256 matched on controller",
        ),
        "I13": InvariantEntry(
            how="canary tokens verified across trials",
            probe="cross-trial canary scan clean",
        ),
        "I14": InvariantEntry(
            how="host handover hygiene: stop processes, delete temporary user and user home, "
            "clean /tmp and caches, verify running process baseline",
            probe="handover check confirms temporary user gone and process list clean",
        ),
        "I15": InvariantEntry(
            how="judge session isolated per trial",
            probe="judge session id is trial-scoped",
        ),
        "I16": InvariantEntry(
            how="host system metrics and execution traces recorded to audit log",
            probe="system probe logs present in trial artifacts",
        ),
    }


def device_pool_isolation_profile() -> IsolationProfile:
    """Return the IsolationProfile for the device-pool provider."""
    return IsolationProfile(
        provider="device-pool",
        declared_isolation=Isolation.L4,
        invariants=device_pool_invariants(),
    )


def perf_host_isolation_profile() -> IsolationProfile:
    """Return the IsolationProfile for the perf-host provider."""
    return IsolationProfile(
        provider="perf-host",
        declared_isolation=Isolation.L4,
        invariants=perf_host_invariants(),
    )


@lru_cache(maxsize=1)
def load_profile_yaml() -> dict[str, object]:
    """Read the bundled isolation_profile.yaml."""
    ref = resources.files(_PACKAGE).joinpath(_PROFILE_FILENAME)
    return yaml.safe_load(ref.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def read_probe_script(name: str) -> str:
    """Read a probe script from the probes resource directory."""
    ref = resources.files(_PACKAGE).joinpath(f"probes/{name}")
    return ref.read_text(encoding="utf-8")
