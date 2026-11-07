# evalspleef-sbx-device-pool

HarmonyOS device pool provider (`device-pool`) and bare-metal performance host provider (`perf-host`) for EvalSpleef. Implements the sandbox SPI (TECH-DESIGN section 7.4.9, PRD FR-SBX-09) with isolation L4, return hygiene enforcement, pre-run system probes, signed artifact validation, and conformance targets.

## Verification status

**Not verified on a real host**: the tests and conformance suite run against a fake `hdc` executable (Python script simulating device CLI responses) and a fake system probe object (`FakeSystemProbe`). Physical HarmonyOS devices and bare-metal performance hosts were not attached during this verification run.

## Entry points

Declared in `pyproject.toml`:
- `evalspleef.sandbox_providers`:
  - `device-pool = evalspleef_sbx_device_pool.provider:DevicePoolProvider`
  - `perf-host = evalspleef_sbx_device_pool.perf:PerfHostProvider`
- `evalspleef.conformance_targets`:
  - `device-pool = evalspleef_sbx_device_pool.conformance:build_targets`

## DevicePoolProvider (`device-pool`)

Manages physical HarmonyOS devices from a YAML inventory (`serial`, `model`, `os_version`, `state`):
- **Leasing & Transitions**: `free` -> `in_use` -> `handover_check` -> `free` (or `quarantined` on hygiene failure).
- **Baseline Check**: Baseline package list and sha256 hash verified at handover.
- **HDC Action Whitelist**: Only whitelisted commands permitted over `hdc` (`install`, `uninstall`, `list targets`, `shell aa start`, `shell aa force-stop`, `shell hilog`, `shell snapshot_display`, `shell bm dump`, `file recv`, `file send`, `shell reboot`). Unlisted actions are rejected with permission errors.
- **Return Hygiene Checklist**: At destroy time:
  1. Force-stop running applications.
  2. Uninstall added packages.
  3. Clean `/data/local/tmp/*`.
  4. Compare package list and sha256 baseline hash.
  5. Reboot device.
- **Quarantine & Incident Reporting**: Any device failing return hygiene is marked `quarantined`, removed from the active pool, and logged in `incidents` with package diff and hashes. Devices never go back to a warm pool.

## PerfHostProvider (`perf-host`)

Bare-metal performance host provider for frame-rate and benchmarking workloads:
- **Isolation L4 & Concurrency**: Strictly `max_concurrency = 1`.
- **System Preconditions**: Evaluates preflight probes before creation:
  - `load1 < 2.0`
  - `LowPowerMode` disabled
  - Temperature `< 80.0°C`
- **Signed Artifacts**: Enforces sha256 verification against a declared artifact manifest before running binaries.
- **Temporary User & Cleanup**: Creates an isolated per-trial user and directory, wiped completely on destroy.

## Conformance

Run with:
```bash
uv run pytest --spleef-provider device-pool-fake --spleef-report /tmp/spleef-device-pool-fake-report.json packages/adapters/evalspleef-sbx-device-pool -q
```
Targets exposed:
- `device-pool-fake` (`fake=True`)
- `perf-host-fake` (`fake=True`)
Fault injectors: `lose_sandbox`, `fail_create`, `expire_lifetime`.
