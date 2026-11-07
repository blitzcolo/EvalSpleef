# evalspleef-sbx-docker

Docker sandbox provider (`docker`) for EvalSpleef: one hardened container per
trial on a local Docker daemon, driven through the Engine API via
[`aiodocker`](https://github.com/aio-libs/aiodocker). Implements the sandbox
SPI (TECH-DESIGN section 7.4.2) and ships the isolation profile (FR-SBX-11),
the Linux probe set, and a conformance target for the real local daemon.

**Verified on a real host**: the conformance suite and the package tests ran
against a live Docker Desktop daemon (Engine 29.9.2, linux/arm64). Behaviour
on native Linux dockerd is designed to be identical but is not verified here.

## Entry points

- `evalspleef.sandbox_providers`: `docker = evalspleef_sbx_docker.provider:DockerProvider`
- `evalspleef.conformance_targets`: `docker = evalspleef_sbx_docker.conformance:build_targets`

## Provider configuration

`DockerProvider(instance, config, secrets)` reads these config keys:

| key | default | meaning |
| --- | --- | --- |
| `runtime` | `runc` | container runtime; `runsc` (gVisor) raises the declared isolation to L2 |
| `egress_network` | unset | name of an internal Docker network used for `docs_whitelist` / `package_mirror` policies that carry `via_proxy` |
| `max_concurrency` | 16 | live sandboxes at a time; excess `create` calls raise `QuotaExceeded` |
| `max_lifetime_s` | 7200 | capability ceiling for `spec.max_lifetime_s` |
| `pids_limit` | 256 | container `pids.max` |
| `ttl_sweep_interval_s` | 30 | interval of the background orphan/TTL sweep |
| `engine_url` | env default | aiodocker engine URL override |

## Hard isolation defaults (PRD appendix D.2)

Every container is created with:

- read-only root filesystem; writable space is tmpfs only: `/tmp` (sticky),
  `/home/<run_as_user>` and `/workspace` (mode 0777, since tmpfs ownership
  needs a numeric uid and the SPI passes a user name)
- `user=<run_as_user>` (non-root), `cap_drop ALL`, `no-new-privileges`,
  an init process, `pids_limit`, `nano_cpus`, `memory` with equal
  `memory_swap` (swap disabled), cgroup v2 enforcement of cpu/memory/pids
- no host bind mounts, no Docker socket, no port publishing
- labels from `spec.labels` plus `spleef.provider` and a
  `spleef.expires_at` TTL label; `list`/`reap` filter by label and a
  background sweep destroys expired containers

Network policy mapping (FR-SBX-03): `off` → Docker `none` network,
`full` → default bridge. `docs_whitelist`/`package_mirror` are only offered
when `egress_network` is configured and the policy carries `via_proxy`
(allowlist enforcement and URL logging live in the egress proxy, ADR-v3-07);
every other allowlist combination is rejected at create with
`PolicyNotEnforceable` — the provider never degrades silently.

## exec, timeouts and files

- `exec` uses `exec_create`/`exec_start` over the hijacked raw stream with
  demultiplexed stdout/stderr, incremental `on_output` callbacks, cwd/env/user
  support, and stdin (EOF is signalled with a half-close so the command output
  still arrives).
- Timeouts are enforced by wrapping the command in GNU
  `timeout --kill-after=5s` when the image provides it (detected once per
  sandbox); `timeout` signals the whole process group, so background children
  die too. The result is `exit_code == 124` with `timed_out=True`. On images
  without coreutils a client-side watchdog kills matching process trees via
  `/proc` as a best effort.
- Output above `max_output_bytes` is truncated head-and-tail and flagged with
  `truncated=True`.
- `upload`/`download` move tar streams over exec (`tar -xf -` on stdin,
  `tar -cf -` on stdout) instead of `put_archive`/`get_archive`: dockerd
  refuses the archive endpoints for read-only-rootfs containers (HTTP 400
  "container rootfs is marked read-only") and cannot resolve tmpfs paths for
  them. Permission bits and mtimes are preserved; uploads reject symlinks
  escaping the tree; downloads apply the I11 sanitisation (no absolute or
  escaping symlinks, no fifo/socket/device, no setuid/setgid, size caps).

## Isolation profile and probes

`isolation_profile.yaml` and `evalspleef_sbx_docker.isolation.isolation_profile()`
carry the I1–I16 profile (no waivers); `probes/linux.sh` is the Linux preflight
probe set (non-root, no sudo, read-only root, tmpfs /tmp, private network
namespace, no Docker socket, minimal capabilities, cgroup limits, neutral
identity). The tests enforce that the YAML and the Python profile stay in sync.

## Conformance

```
uv run pytest -p evalspleef.conformance.plugin \
    --spleef-provider local-docker \
    --spleef-report /tmp/spleef-local-docker-report.json \
    packages/adapters/evalspleef-sbx-docker -q
```

`build_targets()` builds `spleef-test-sandbox:local` once (derived from
`python:3.12-slim` with an unprivileged `agent` user, `/workspace`, probe
tools, no sudo; pulls the base only if absent) and returns the `local-docker`
target with the `lose_sandbox`, `expire_lifetime` and `fail_create` injectors.
The target's cleanup removes every container, the test image, and the base
image if this target pulled it. `reachable_from_sandbox` is the bridge gateway
address so the network canary cases can observe allow/block behaviour.

Not applicable for this provider (by capability declaration): C-NET-02 (no
native hostname allowlist — proxy only), C-NET-05 (no dynamic network policy),
C-SNAP-01/02, C-GUI-01 and C-PORT-01 (snapshot/GUI/port-expose not declared).

## Known limitations

- `tar` must exist in task images for upload/download (true for the Debian
  required package set; the conformance test image has it).
- Exec timeouts map any wrapped exit code 124 to `timed_out=True`; a command
  that genuinely exits 124 inside a `timeout_s` window is indistinguishable.
- Disk quotas are not enforced (no `storage-opt size`); `enforces_limits`
  declares cpu/memory/pids only.
- Anonymous-volume `/workspace` would give stricter tmpfs-style ownership, but
  tmpfs keeps the C-ISO-04 mount-table guarantee portable across dockerd
  storage backends.
