# evalspleef-sbx-apple-container

Sandbox provider `apple-container` for EvalSpleef: one lightweight Linux VM per
trial on macOS 26 with Apple silicon, driven through the `container` CLI
(`run`, `exec`, `cp`, `rm`, `ls`, `network`). Implements the sandbox SPI
(TECH-DESIGN section 7.4.4). Every container is its own VM, so the declared
isolation is **L3** and containers do not consume the macOS guest VM budget of
two.

## Verification status

**Not verified on a real host.** No macOS 26 machine or real `container` CLI was
available while this adapter was written. Everything below ran against a fake
`container` executable: a stdlib Python script (`fake_container.py`) copied onto
a temporary `PATH`, backed by local directories and host subprocesses. The fake
simulates VM filesystems, cgroups, mounts, the internal network and the CLI exit
codes; it is not the Apple runtime.

- `packages/adapters/evalspleef-sbx-apple-container/tests/` — unit tests whose
  provider, sandbox and conformance wiring all talk to the fake.
- The conformance target `apple-container-fake` sets `fake=True`, so every report
  row is evidence against a double, never "verified on a real host".
- The CLI surface used here was checked against the upstream documentation listed
  under "Sources" (docs current branch plus the release notes for macOS 26
  support). Real flag behaviour, boot timing, `container cp` semantics (symlinks,
  directory targets, permission bits) and the exec error text of a missing
  container are still unverified on hardware.
- The real CLI's "container not found" detection does not parse error text: after
  a non-zero exec the provider asks `container inspect` whether the container
  still exists. That fallback is verified against the fake only.

## Entry points

- `evalspleef.sandbox_providers`:
  `apple-container = evalspleef_sbx_apple_container.provider:AppleContainerProvider`
- `evalspleef.conformance_targets`:
  `apple-container = evalspleef_sbx_apple_container.conformance:build_targets`

## Requirements and provider configuration

macOS 26 or later on Apple silicon, with the `container` system service started
(`container system start`). `AppleContainerProvider(instance, config, secrets)`
reads:

| key | default | meaning |
| --- | --- | --- |
| `container_bin` | `container` | CLI executable; the tests and the conformance target point this at the fake |
| `cli_env` | `{}` | extra environment for every CLI subprocess (used by the fake to find its state directory) |
| `egress_proxy` | unset | host proxy address; whitelist network modes are only offered when this is set |
| `internal_network` | `spleef-internal` | internal network created with `network create --internal` for `off` and whitelist modes |
| `open_network` | `default` | network used for `full` mode; the system creates it |
| `max_concurrency` | 8 | live containers at a time; excess `create` calls raise `QuotaExceeded` |
| `max_lifetime_s` | 7200 | capability ceiling; a larger `spec.max_lifetime_s` is clamped |
| `boot_timeout_s` | 90 | upper bound for create plus the readiness probe |
| `transfer_timeout_s` | 300 | timeout for each `container cp` call |
| `ttl_sweep_interval_s` | 30 | interval of the background TTL/orphan sweep |

## Capabilities and limits

`capabilities()` declares exactly what this adapter can enforce:

| field | value |
| --- | --- |
| OS / arch | `linux` only, `arm64` only |
| isolation | L3 (one VM per container) |
| network modes | `off`, `full`; plus `docs_whitelist` / `package_mirror` when `egress_proxy` is configured |
| allowlist kinds | none — host/cidr allowlists are enforced by the egress proxy, not by the adapter |
| streaming / stdin | yes / yes |
| snapshot / GUI / GPU / port expose / host mounts | none / none / no / no / no |
| enforced limits | `cpu`, `memory` (`enforces_limits`) |
| `max_concurrency` / `max_lifetime_s` | from config |

Network mapping (TECH-DESIGN section 7.4.4): `off` attaches the container to an
**internal** network (`--internal`); `full` attaches it to the open `default`
network. An allowlist policy requires both a configured `egress_proxy` and
`policy.via_proxy`; the container sits on the internal network and every exec
receives `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` pointing at the proxy. A
host/cidr allowlist on `off`/`full`, or a whitelist policy without the proxy, is
rejected at create with `PolicyNotEnforceable` — the adapter never degrades
silently.

## Container lifecycle

`create()` runs a detached container with:

- `--read-only` and tmpfs mounts for `/tmp`, `/workspace` and `/home/<run_as_user>`
- `--user <run_as_user>`, `--cap-drop ALL`, `--init`, neutral container name
  (`c-<hex>`), `--workdir /workspace`
- `--cpus` and `--memory` from `spec.resources`
- every `spec.labels` entry as `--label`, plus `spleef.provider`,
  `spleef.created_at` and `spleef.expires_at` (the lifetime guard)
- the OCI image as `ref@sha256:...` when the template carries a digest

After `run`, a readiness probe (`container exec <id> true`) runs until it
succeeds or `boot_timeout_s` expires; on failure the container is removed and
`SandboxCreateError` is raised. `list()` reads `container list --all --format
json` and filters labels client-side (the CLI has no label filter). `reap()`
uses the same list and removes containers past `spleef.expires_at` or older than
`older_than_s`; the background sweep does the same for the provider's instances.
`destroy()` is idempotent (`rm --force`; a missing container counts as removed).

## exec, timeouts and files

- `exec` runs `container exec` with per-call `--user`, `--workdir` and `--env`;
  a `str` command becomes `sh -lc`. The CLI subprocess starts a new session and
  its stdout/stderr are streamed through capped buffers with `on_output`
  callbacks. Output above `max_output_bytes` is truncated head-and-tail and
  flagged.
- A timeout kills the **whole CLI process group** (`SIGKILL`) and returns
  `exit_code == 124` with `timed_out=True`. Whether the guest process tree also
  dies when a real CLI connection is severed is unverified on hardware.
- If an exec exits non-zero, the provider checks `container inspect`; a vanished
  container raises `SandboxLost` instead of returning the CLI's error code.
- `upload` and `download` use `container cp`; parents are created with
  `mkdir -p`. Downloads are re-validated locally (invariant I11): absolute or
  escaping symlinks and special files are rejected, setuid/setgid bits are
  cleared, and `max_bytes` is enforced.

## Isolation profile and probes

`isolation_profile.yaml` (shipped) and
`evalspleef_sbx_apple_container.isolation.isolation_profile()` carry the I1-I16
profile. I7 is waived for pids and disk only: the CLI enforces cpu and memory,
but has no per-VM pids or disk quota; the waiver records the reason, the
compensating controls and an expiry date. The tests keep the YAML and the Python
definition in sync.

`probes/linux.sh` is the Linux preflight probe set (non-root, no sudo, read-only
root, tmpfs workspace, no default route in `off` mode, no runtime socket,
minimal capabilities, cgroup cpu/memory, neutral hostname and environment). The
control plane feeds it to a sandbox over exec stdin.

## Conformance

```
uv run pytest -p no:spleef-conformance -p evalspleef.conformance.plugin \
    --spleef-provider apple-container-fake \
    --spleef-report /tmp/spleef-apple-container-fake-report.json \
    packages/adapters/evalspleef-sbx-apple-container -q
```

The leading `-p no:spleef-conformance` is required in this workspace: the
conformance package is installed with a `pytest11` entry point named
`spleef-conformance`, and loading the same module explicitly under its import
name makes pytest abort with "Plugin already registered under a different
name". Disabling the autoloaded copy keeps the explicit module and the
`--spleef-provider` options active.

`build_targets()` copies the fake CLI to a temporary directory, creates a
provider pointed at it and returns the `apple-container-fake` target with the
`lose_sandbox`, `expire_lifetime` and `fail_create` injectors, the
`reachable_from_sandbox = "127.0.0.1"` observer and a cleanup that removes the
temporary tree.

Not applicable for this provider (by capability declaration, not by failure):
C-NET-02 (no native hostname allowlist — proxy only), C-NET-05 (no dynamic
network policy), C-CONC-01 (in-process double does not model the vendor
concurrency limit), C-SNAP-01/02, C-GUI-01 and C-PORT-01. C-ISO-05 is not
applicable because the isolation profile waives I7 for pids and disk.

## Known limitations

- Task images must provide `sleep` for the idle init process and the
  `run_as_user` account; the provider does not synthesize either.
- `container cp` semantics (directory targets, symlink preservation, permission
  bits) are assumed and exercised against the fake only.
- Disk quotas and pids limits are not enforced (`I7` waiver).
- `vmnet` pinning, Rosetta and nested virtualization are not used.
- The internal network is created once and not deleted when the provider is
  closed.

## Sources

- <https://github.com/apple/container> (requirements, macOS 26, Apple silicon)
- <https://raw.githubusercontent.com/apple/container/main/docs/command-reference.md>
  (`run`, `exec`, `cp`, `rm`, `list`, `network create --internal`, options)
- <https://raw.githubusercontent.com/apple/container/main/docs/networking.md>
  (internal networks, publish, host integration)
- <https://raw.githubusercontent.com/apple/container/main/docs/container-inspection.md>
  (`list --format json`, `inspect` shape)
- <https://raw.githubusercontent.com/apple/container/main/docs/host-integration.md>
