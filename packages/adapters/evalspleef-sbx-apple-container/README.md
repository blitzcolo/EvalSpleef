# evalspleef-sbx-apple-container

Sandbox provider `apple-container` for EvalSpleef: one lightweight Linux VM per
trial on Apple silicon, driven through Apple's `container` CLI (`run`, `exec`,
`delete`, `list`, `inspect`, `network`, `system`). Implements the sandbox SPI
(TECH-DESIGN section 7.4.4). Every container is its own Virtualization.framework
VM with its own guest kernel, so the declared isolation is **L3** and containers
do not consume the macOS guest VM budget of two.

## Verification status

**Verified on a real host** through the `apple-container-live` conformance
target and the gated live tests in `tests/test_live_carrier.py`:

| | |
| --- | --- |
| host | macOS 27.2 (build 26B123), Apple silicon (arm64) |
| CLI | `container` 1.6.1 (Homebrew), apiserver 1.6.1 |
| guest kernel | Apple's default kernel, Linux 6.18.35 aarch64 (`vmlinux-6.18.35-197-debug`) |
| image | `docker.io/library/python:3.12-slim`, run as `nobody`, pinned by digest |
| live conformance | 28 passed, 0 failed, 7 not applicable (by declared capability), 2026-11-10 |

Inside a live sandbox created with `cpus=2, memory_mb=1024`: `uname -r` prints
`6.18.35`, `id -un` prints the requested user, `cpu.max` is `200000 100000`,
`memory.max` is `1073741824`. The VM itself shows `nproc` = 3 (one vCPU of
overhead) and slightly more `MemTotal` than requested; the cgroup limits are the
enforced ones.

Two trial-flow paths, uploading a nested directory **as** `/workspace` and
writing under `/verify` in a verifier-role sandbox, failed on the real CLI before
the fixes (found by the AC-03 parity run, `docs/evidence/ac03-carriers.md`). They
are covered by live tests in `tests/test_live_carrier.py` and now by the
conformance cases C-FILE-05 and C-FILE-06. C-NET-01 probes the gateway of every
network `container network list` reports, not only the `default` one: with `off`
mapped back to the internal network it fails on the internal gateway and the
host's LAN address while the `default` gateway stays unreachable, which is how
the old host-only mapping passed the single-address probe.

Still verified against the fake CLI only (`fake_container.py`):

- the proxy-backed whitelist modes (`docs_whitelist`, `package_mirror`) and the
  internal network they use: no egress proxy was available for the live run;
- `reap()`/TTL sweep timing over long periods (the sweep path itself ran live
  through C-LIFE-03 with the clock moved past the lifetime);
- images without `sh`, `tar` or `sleep`, and busybox-based images (the live
  image is Debian with GNU tar and dash).

The fake mirrors the real CLI where the adapter depends on it (see the module
docstring): `--network none` is loopback only, an internal network is host-only,
an exec'd process survives a killed CLI, `cp` works on the root filesystem
beneath the tmpfs mounts, only the tmpfs mounts are writable on the read-only
root, `tar -x` over a root-owned mount point fails as the sandbox user, and the
JSON shapes and error texts follow 1.6.1.

## Install

```
brew install container
container system start --enable-kernel-install   # installs Apple's default kernel
container image pull docker.io/library/python:3.12-slim
```

## Entry points

- `evalspleef.sandbox_providers`:
  `apple-container = evalspleef_sbx_apple_container.provider:AppleContainerProvider`
- `evalspleef.conformance_targets`:
  `apple-container = evalspleef_sbx_apple_container.conformance:build_targets`

## Provider configuration

`AppleContainerProvider(instance, config, secrets)` reads:

| key | default | meaning |
| --- | --- | --- |
| `container_bin` | `container` | CLI executable; the tests and the fake target point this at the fake |
| `cli_env` | `{}` | extra environment for every CLI subprocess (used by the fake to find its state directory) |
| `name_prefix` | `c-` | container-name prefix; the name is also the guest **hostname**, so keep it neutral |
| `egress_proxy` | unset | host proxy address; whitelist network modes are only offered when this is set |
| `internal_network` | `spleef-internal` | `--internal` (host-only) network created for the whitelist modes |
| `open_network` | `default` | network used for `full` mode; the system creates it |
| `max_concurrency` | 8 | live containers at a time; excess `create` calls raise `QuotaExceeded` |
| `max_lifetime_s` | 7200 | capability ceiling; a larger `spec.max_lifetime_s` is clamped |
| `boot_timeout_s` | 90 | upper bound for `run` plus the readiness probe |
| `transfer_timeout_s` | 300 | timeout for each upload or download stream |
| `ttl_sweep_interval_s` | 30 | interval of the background TTL/orphan sweep |

## Capabilities and isolation

`capabilities()` declares exactly what the carrier enforces:

| field | value |
| --- | --- |
| OS / arch | `linux` only, `arm64` only |
| isolation | L3: a VM and guest kernel per container |
| network modes | `off`, `full`; plus `docs_whitelist` / `package_mirror` when `egress_proxy` is configured |
| allowlist kinds | none: host/cidr filtering would be the proxy's job, the CLI has no filter |
| streaming / stdin | yes / yes |
| snapshot / GUI / GPU / port expose / host mounts | none / none / no / no / no |
| enforced limits | `cpu`, `memory` |

Network mapping, as verified on the real CLI:

- `off` runs the VM with `--network none`: only `lo`, no route, no DNS, the host
  unreachable.
- `full` attaches the `default` vmnet NAT network (internet and host reachable).
- `container network create --internal` makes a **host-only** network: no route
  off the host, but every service listening on the host's address there is
  reachable. It is therefore never used for `off`; the whitelist modes use it
  with `HTTP(S)_PROXY`/`ALL_PROXY` pointing at the proxy, and the proxy host must
  expose nothing else on that address.
- A host/cidr allowlist, or a whitelist mode without `egress_proxy` and
  `policy.via_proxy`, is rejected at create with `PolicyNotEnforceable`.

Resource limits: `--cpus` sets the guest cgroup `cpu.max` (cpus x 100000) and
`--memory` sets `memory.max` exactly. There is no pids limit (`pids.max` is
`max`) and no disk quota, so I7 is waived for pids and disk (reason, compensating
controls and expiry are recorded). Compensation: pid exhaustion stays inside the
sandbox's own kernel, and every writable path is tmpfs charged to the memory
cgroup (a write past `memory.max` is OOM-killed, verified live), so writable
storage is bounded by `memory_mb`. C-ISO-05 is not applicable because of the waiver.

User mapping: the init process and every exec run as `spec.run_as_user`
(`--user`); the account must exist in the image (the CLI refuses an unknown user
when the VM starts). `--cap-drop ALL` leaves effective capabilities empty, root
included. The root filesystem is a read-only ext4 block device (`--read-only`);
`/tmp`, `/workspace` and `/home/<run_as_user>` are tmpfs, and verifier-role
sandboxes also get a `/verify` tmpfs (the evaluator's default verify root). Tmpfs
mount points are root-owned with mode 1777: the sandbox user can write in them
but cannot change their mode or times.

`isolation_profile.yaml` and `isolation.isolation_profile()` carry I1-I16 and are
kept identical by the tests. I1-I9 have executable `$ <command>` probes that hold
in every network mode; `tests/test_live_carrier.py` runs them in real `off` and
`full` sandboxes. `probes/linux.sh` is the stdin-fed probe set (its neutral
hostname check flags a hostname that contains `spleef`, which a lab name prefix
such as `spleef-applelane-` does on purpose).

## Container lifecycle

`create()` runs `container run --detach` with `--read-only`, the tmpfs mounts
(plus `/verify` for `role == "verifier"`), `--user`, `--workdir /workspace`, `--cap-drop ALL`, `--init`, `--cpus`,
`--memory`, the network above, every `spec.labels` entry plus `spleef.provider`,
`spleef.created_at` and `spleef.expires_at`, and the image as `ref@sha256:...`.
A readiness probe (`container exec <id> true`) runs until it succeeds or
`boot_timeout_s` expires; on failure the container is removed and
`SandboxCreateError` is raised. A failing `run` (for example a taken name) leaves
any existing container alone. `list()` parses `container list --all --format
json` and filters labels client-side. `reap()` and the background sweep remove
containers past `spleef.expires_at`; `sweep_expired(now=...)` runs that TTL path
on demand. `destroy()` is idempotent (`delete --force`; a missing container
counts as removed).

## exec, timeouts and files

- `exec` runs `container exec` with per-call `--user`, `--workdir` and `--env`;
  a `str` command becomes `sh -lc`. Output is streamed through capped buffers.
- Killing the `container exec` client does **not** stop the guest process, so
  each command runs under a small `sh` wrapper that records its pid in
  `/tmp/.exec-<hex>.pid`. The guest process is a session and process-group
  leader; on timeout the provider kills that group inside the VM
  (`kill -KILL -<pid>`) and returns exit code 124.
- A non-zero exec whose container is gone raises `SandboxLost`.
- Files move as tar streams over `exec` stdin/stdout, as the sandbox user:
  upload unpacks with `tar -x` (umask 000, so modes survive). A directory is
  packed without its own entry and unpacked **into** the remote path, so uploading
  a tree as `/workspace` never touches the root-owned mount point (the remote
  directory itself gets the sandbox user's default mode). Download packs with
  `tar -c` and unpacks locally with `tarfile`'s data filter plus the I11 checks
  (no absolute or escaping links, no special files, setuid/setgid cleared,
  `max_bytes` enforced). `container cp` is not used: it reads and writes the VM's
  root filesystem beneath the tmpfs mounts and past `--read-only`.
- Task images must provide `sh`, `tar`, `sleep` and the `run_as_user` account.

## Execution environment

`environment()` reports, for reproducibility: the host (this Mac, through
`local_host_facts`), the runtime (name and version from `container --version`),
and the guest: `linux`, the arch, and the kernel release read from the
`Linux version` banner of the CLI's default kernel image
(`<appRoot>/kernels/default.kernel-<arch>`), which is what `uname -r` prints in a
sandbox. `extra` holds the app and install roots, the apiserver version and
build, the vminit image, and the default kernel image name and sha256. Guest
distribution, CPUs and memory depend on the image and the spec, so they stay
unreported. Example from the verified host:

```
runtime  container 1.6.1
guest    linux arm64, kernel 6.18.35
extra    default_kernel_image=vmlinux-6.18.35-197-debug,
         vminit_image=ghcr.io/apple/containerization/vminit:0.53.0, apiserver_version=1.6.1
```

## Conformance

Fake target (always built, `fake=True`):

```
.venv/bin/pytest --spleef-provider apple-container-fake packages/adapters/evalspleef-sbx-apple-container -q
```

Live target, built only when the `container` executable is on `PATH` **and**
`SPLEEF_APPLE_CONTAINER_IMAGE` names a locally pulled image (a plain `pytest`
elsewhere never creates a VM):

```
SPLEEF_APPLE_CONTAINER_IMAGE=docker.io/library/python:3.12-slim \
    .venv/bin/pytest --spleef-provider apple-container-live \
    packages/adapters/evalspleef-sbx-apple-container -q
```

Optional: `SPLEEF_APPLE_CONTAINER_USER` (account in the image, default `nobody`)
and `SPLEEF_APPLE_CONTAINER_NAME_PREFIX` (default `spleef-conformance-`). The
same gate enables `tests/test_live_carrier.py`. The live target pins the image to
the digest the local store reports and uses the `default` network's gateway as
the canary address.

Injectors drive the CLI in both targets: `lose_sandbox` is an out-of-band
`container delete --force`; `fail_create` creates a container holding the next
pinned name so the real `run` fails ("already exists"); `expire_lifetime` runs
the adapter's own TTL sweep with the clock past the lifetime.

Not applicable on the live target, by declared capability: C-NET-02 (no hostname
allowlist, no proxy configured), C-NET-05 (no dynamic network policy), C-ISO-05
(I7 waived for pids and disk), C-SNAP-01/02, C-GUI-01, C-PORT-01. C-CONC-01 runs
on the live target (declared `max_concurrency` 3) and is not applicable on the
fake.

## Known limitations

- No disk quota or pids limit (I7 waiver); writable storage is RAM-backed tmpfs.
- The container name is the guest hostname; only a neutral `name_prefix` keeps
  the evaluation identity out of the guest.
- The internal network for whitelist modes is created once and not deleted when
  the provider closes.
- `vmnet` pinning, Rosetta and nested virtualization are not used.

## Sources

- <https://github.com/apple/container> and `container <subcommand> --help` of 1.6.1
- <https://raw.githubusercontent.com/apple/container/main/docs/command-reference.md>
- <https://raw.githubusercontent.com/apple/container/main/docs/networking.md>

## Declared paths and read-only uploads

The provider declares `writable_paths` and `read_only_upload`. Each declared path becomes
`--tmpfs <path>:size=<size_mb>m,mode=1777` (root-owned, sticky, charged to the VM memory); the
role mounts nothing by itself. A read-only upload is unpacked with `container exec --user root`,
which works with `--cap-drop ALL` on container 1.6.1: root owns what it unpacks, the sandbox user
cannot write, delete, rename or chmod it (checked on the real CLI on 2026-11-10). The fake CLI
models root ownership with the user-immutable file flag (macOS).
