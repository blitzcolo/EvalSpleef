# evalspleef-sbx-windows-sandbox

Sandbox provider backed by the local `wsb` CLI of **Windows Sandbox**
(TECH-DESIGN section 7.4.7, PRD FR-SBX-08). It exists for task authors who want
to self-test a task on their own Windows 11 Pro / Server workstation. It is not
an evaluation carrier.

## Developer self-test only

`WindowsSandboxProvider.dev_only = True`. Windows Sandbox allows one instance
per host, cannot stream output, and cannot enforce an egress allowlist, so the
control plane keeps this adapter out of every formal batch route. Nothing in
this package registers a route; a batch that names `windows-sandbox` is a
configuration error. The capability object advertises isolation `L3` but a
bounded instance lifetime (`max_lifetime_s`, default 1800 s, further capped by
`SandboxSpec.max_lifetime_s`).

## How the wsb limits are handled

| wsb limit | adapter behaviour |
| --- | --- |
| `wsb start --config <xml>` returns the instance id | the XML maps one per-instance host folder into the guest and sets `<Networking>Enable/Disable</Networking>` |
| `wsb exec` returns only an exit code | the command is wrapped so stdout and stderr go to `.spleef/<run>.stdout` / `.stderr` inside the mapped folder; the adapter reads them back after the command; `capabilities().streaming` is `False` and `on_output` fires once at the end |
| one instance per host | `capabilities().max_concurrency == 1`; a second `create()` raises `QuotaExceeded` |
| network is fully off or fully on | `net_modes == {off, full}`, `allowlist_kinds == frozenset()`; any `docs_whitelist`/`package_mirror` mode, `allow_hosts`, `allow_cidrs` or `via_proxy` raises `PolicyNotEnforceable` |
| `wsb stop` | `destroy()` is idempotent; an unknown instance still succeeds |
| fixed guest account | commands run as the built-in `WDAGUtilityAccount`; `exec(user=...)` with another account raises `CapabilityNotSupported`, as does `stdin` |

Timeouts kill the whole client process group (`start_new_session=True`,
`SIGKILL` to the group) and return `exit_code=124` with `timed_out=True`.

## Instances, labels and lifetime

`create()` writes the spec labels plus `spleef.provider` and
`spleef.expires_at` into `state_dir/instances/<id>.json`, next to the
per-instance mapped workspace. `list(labels)` matches on every requested label
and `reap(labels, older_than_s)` stops matching instances that are older than
the threshold or past `spleef.expires_at`. `exec` raises `SandboxLost` once the
metadata is gone or the lifetime has expired. `templates` must be of kind
`vhdx`; only the digest is recorded, the guest image is the host's Windows
Sandbox image.

## Isolation profile

`isolation_profile()` returns I1-I16 with `declared_isolation=L3` and
`verified_isolation=None`. Invariants a Windows Sandbox cannot meet are
waivers that carry a reason, a compensating control, an approver and an expiry
date (`I2` read-only rootfs, `I3` host path, `I5` host mounts, `I7` resource
limits, `I16` egress logging). The profile is mirrored in
`isolation_profile.yaml`; a test keeps both in sync. `probes/windows.ps1` is the
guest preflight probe set and is run through `exec` by
`WindowsSandboxProvider.run_guest_probes(sandbox)`.

## Configuration

```yaml
providers:
  windows-sandbox:
    kind: sandbox
    adapter: windows-sandbox
    instance: dev-workstation
    config:
      wsb_path: wsb                     # looked up on PATH unless absolute
      state_dir: C:\ProgramData\evalspleef\wsb
      max_lifetime_s: 1800
      exec_timeout_s: 600
```

`guest_os` (default `windows`) and `guest_workspace` (default
`C:\Users\WDAGUtilityAccount\Desktop\workspace`) are advanced settings.
`guest_os: linux` exists only for the fake harness described below: the fake
`wsb` executes commands with `/bin/sh` on a POSIX host, so the conformance kit's
POSIX command variants are the ones that can run. A real deployment never sets
it.

## Conformance

```
uv run pytest -p evalspleef.conformance.plugin \
    --spleef-provider windows-sandbox-fake \
    --spleef-report /tmp/spleef-windows-sandbox-fake-report.json \
    packages/adapters/evalspleef-sbx-windows-sandbox -q
```

`build_targets()` installs the shipped fake `wsb` (a Python script,
`fake_wsb.py`) on a temporary `PATH` and returns the
`windows-sandbox-fake` target with `fake=True`, `guest_os=linux` and the
`lose_sandbox`, `expire_lifetime` and `fail_create` injectors. Cleanup removes
every instance and restores `PATH`.

The network-isolation cases are not applicable because the target supplies no
`reachable_from_sandbox` observer (the fake has no network isolation), and
snapshot/GUI/port cases are not declared. All other lifecycle, exec, transfer
and fault cases run.

## Verification status

**Not verified on a real host.** Windows Sandbox only runs on Windows, and this
package was built and tested on macOS. Everything here ran against the fake
`wsb` executable shipped in `fake_wsb.py`, which emulates `start`, `exec` and
`stop` over a local directory using `/bin/sh`; the conformance target is
`fake=True` and uses `guest_os=linux` solely because of that fake.

Verified against the fake (see the test suite and the conformance JSON report):

- create / exec / upload / download / destroy, idempotent destroy;
- exit codes, stdout/stderr separation via the mapped folder, truncation,
  `on_output` once at the end;
- timeout kills the whole process tree and reports exit code 124;
- one instance per host, labels in `list()`/`reap()`, lifetime expiry;
- allowlist and proxy policies rejected with `PolicyNotEnforceable`;
- symlink and path escapes rejected on upload/download.

Not verified / not exercised:

- the real `wsb` CLI's config XML, `ExistingLogin` session handling and the
  real guest file layout; `wsb exec -r ExistingLogin` requires a prior
  `wsb connect` session on a real host and the adapter does not create one;
- whether killing the host-side `wsb exec` process also terminates the guest
  process tree (the fake shares the process group, a real guest would not);
- the PowerShell probe script (`probes/windows.ps1`) against a real guest; only
  its plumbing through `exec` is tested;
- memory enforcement (`<MemoryInMB>` is written but not verified) and the
  advertised L3 isolation on a real Windows host.

## Known limitations

- The mapped workspace is a writable host directory; there is no read-only
  system drive, no tmpfs layer and no resource limiting (see the waivers).
- `exec` without `timeout_s` falls back to `exec_timeout_s` (600 s).
- Template kind must be `vhdx`; the digest is recorded but the host image is
  what actually boots.
- The provider does not create the `wsb connect` login session needed by
  `-r ExistingLogin`; a real host needs that session established out of band.
