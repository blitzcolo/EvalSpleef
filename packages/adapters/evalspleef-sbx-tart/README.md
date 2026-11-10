# evalspleef-sbx-tart

Tart sandbox provider (`tart`) for EvalSpleef: one Apple Silicon VM per trial,
cloned from a golden macOS or Linux guest by digest, driven through the local
`tart` CLI with `asyncio.create_subprocess_exec`. Implements the sandbox SPI
(TECH-DESIGN section 7.4.3, PRD FR-SBX-06) and ships the isolation profile
(FR-SBX-11), per-OS probe sets and a conformance target.

## Verification status

**Not verified on a real host.** Every check in this package ran against a fake
`tart` executable written to a temporary directory on `PATH`
(`evalspleef_sbx_tart/_fake_tart.py`). The adapter really spawns processes and
parses CLI output, but no Apple Silicon host, no real macOS/Linux guest and no
real Tart installation was involved. The following remain unverified:

- real `tart clone` / `tart set` / `tart run --net-softnet*` behaviour and flag
  spelling against the current Tart release;
- the guest agent becoming ready, `tart exec` exit-code and stream semantics,
  and the `sudo -u agent -i` path inside a real guest;
- the `--vnc-experimental` flag and the host-side VNC port (the adapter parses a
  `vnc://` URL from `tart run` output and falls back to `vnc_host:vnc_port`);
- the asyncssh fallback against a real SSH server (it is exercised with a fake
  connection object that runs the command locally);
- enforcement of the softnet allow/block rules inside the guest (the fake only
  records the options it received);
- host concurrency, APFS clone cost, disk headroom and the ~25 GB golden image.

## Entry points

- `evalspleef.sandbox_providers`: `tart = evalspleef_sbx_tart.provider:TartProvider`
- `evalspleef.conformance_targets`: `tart = evalspleef_sbx_tart.conformance:build_targets`

## Provider configuration

`TartProvider(instance, config, secrets)` reads these keys:

| key | default | meaning |
| --- | --- | --- |
| `tart_bin` | `tart` | path or name of the Tart CLI |
| `host_macos_slots` | `2` | macOS guests per host; macOS creates beyond it raise `QuotaExceeded`. Linux guests are not limited by this count |
| `max_lifetime_s` | `14400` | capability ceiling for `spec.max_lifetime_s` |
| `start_timeout_s` | `180` | clone/boot budget, including waiting for the guest agent |
| `guest_exec_timeout_s` | `600` | default command budget |
| `ttl_sweep_interval_s` | `30` | interval of the background TTL sweep |
| `state_dir` | `~/.evalspleef/tart` | registry holding labels and the lifetime guard |
| `exec_transport` | `auto` | `auto`, `guest-agent` or `ssh` |
| `ssh_user` | `admin` | management account for the SSH fallback (commands still run as `run_as_user` via sudo) |
| `ssh_port` | `22` | guest SSH port |
| `ssh_key` | unset | `SecretRef` resolved through the secret store |
| `ssh_known_hosts` | unset | passed to asyncssh; `None` disables host-key checking |
| `vnc_host` / `vnc_port` | `127.0.0.1` / `5900` | endpoint used when `tart run` prints no VNC URL |
| `vnc_url_timeout_s` | `5` | how long to wait for the VNC URL in `tart run` output |

## Capabilities and lifecycle

- macOS and Linux guests, arm64 only, isolation L3, `max_concurrency` =
  `host_macos_slots` (2 by default), GUI `vnc`, snapshot `none` for trials,
  no GPU, `enforces_limits` = cpu and memory (fixed with `tart set --cpu` /
  `--memory`).
- `create`: `tart clone <image>@<digest> <vm>` → `tart set <vm> --cpu N
  --memory MB` → `tart run --no-graphics [--vnc-experimental] <net options>
  <vm>` (a long-lived child process) → wait for the guest agent to answer
  `tart exec`. On any failure the VM is stopped and deleted before the error is
  raised as `SandboxCreateError`.
- Commands always run as `spec.run_as_user` through `sudo -n -u <user> -i --
  /bin/sh -lc '<cd workspace && exec ...>'` from the management account; the
  agent account is not expected in sudoers.
- `destroy`: `tart stop` → wait for the run process → `tart delete`; a missing
  VM counts as success, so destroy is idempotent.
- Labels and the lifetime guard live in `<state_dir>/sandboxes/<vm>.json`
  (`spleef.provider`, `spleef.trial`, `spleef.lease`, all `spec.labels`, plus
  `created_at` / `expires_at`). `list`/`reap` filter on those labels, and a TTL
  sweep destroys expired VMs.

## Network policy

| mode | softnet arguments |
| --- | --- |
| `off` | `--net-softnet --net-softnet-block 0.0.0.0/0` |
| `full` | `--net-softnet` (no rules) |
| `docs_whitelist` / `package_mirror` + `via_proxy` | `--net-softnet --net-softnet-allow <proxy-ip>/32` |
| `docs_whitelist` / `package_mirror` + `allow_cidrs` | one `--net-softnet-allow <cidr>` per CIDR |

`allowlist_kinds = {cidr}`: a hostname allowlist is rejected with
`PolicyNotEnforceable` unless `via_proxy` is set. Allowlists on `off`/`full`,
missing rules without `via_proxy`, non-literal proxy addresses and
`via_proxy` together with `allow_cidrs` are also rejected instead of degraded.
`SandboxSpec.env` at create time is rejected: this adapter applies environment
variables per `exec` only.

## exec, timeouts and files

- `exec` streams stdout and stderr through the guest agent (`tart exec <vm> --
  sudo ...`). A string command runs under `/bin/sh -lc`; a list is executed
  without a shell; `cwd` defaults to `/workspace` and `env` is injected with
  `env KEY=VALUE`.
- Timeouts kill the whole process group of `tart exec` (`SIGKILL` to the
  session), report `timed_out=True` and exit code `124`.
- With `exec_transport=auto`, a guest-agent failure whose stderr names the guest
  agent / a refused connection switches that sandbox to asyncssh for the rest of
  its life.
- Upload/download use `tar` over the exec stream (stdin for upload, stdout for
  download). Extraction rejects escaping symlinks, device/fifo members and
  setuid bits, caps sizes, and ignores macOS AppleDouble (`._*`) metadata
  members.

## Isolation profile and probes

`isolation_profile()` returns I1–I16 with `declared_isolation=L3`;
`isolation_profile.yaml` carries the same content for the audit. I2 (writable
guest root disk) is the only waiver: it records the reason, the compensating
controls (fresh clone per trial, digest recorded, canary scan, VM deleted) and
an expiry (`2027-03-01`).

`probes/macos.sh` checks `id -u`, `sudo -n true`, `mount` (no virtiofs/9p/smbfs
host shares), `scutil --get ComputerName`, the environment and the offline test.
`probes/linux.sh` ships the Linux variant. Both are delivered through `exec`
stdin by `run_isolation_probes`.

## Conformance

The conformance target is `tart-fake` and is always `fake=True`. It writes the
fake CLI to a temporary `bin/tart` on `PATH`, points `FAKE_TART_ROOT` at a
temporary directory, and injects the `lose_sandbox`, `expire_lifetime` and
`fail_create` faults.

The fake emulates `clone`, `set`, `run`, `exec`, `stop`, `delete`, `list`, `ip`
and `--version` against local directories and real host subprocesses. Its VNC
listener runs as a detached helper that answers one RFB connection because the
kit's C-GUI-01 reads the endpoint after the sandbox was destroyed; on a real
host the endpoint dies with the VM, so C-GUI-01 would need the kit to connect
inside the sandbox scope.

Not-applicable cases for this target (12): C-NET-01 (no `reachable_from_sandbox`
observer in the fake), C-NET-02 (no hostname allowlist), C-NET-04 (Linux guest
probe, the target runs a macOS spec), C-NET-05 (no dynamic network policy),
C-ISO-02/03/04/05 (Linux-only probes; the target runs a macOS spec),
C-CONC-01 (the kit skips concurrency limits for doubles), C-SNAP-01/02 (snapshot
`none`) and C-PORT-01 (no `port_expose`).

## Known limitations

- Tart has no label/metadata API: labels live in the provider registry, so a
  hostd restart loses the in-memory live set and reap must be driven by labels
  from the control plane.
- Disk size is not enforceable through `tart set`; `enforces_limits` omits disk.
- The guest has no read-only root disk; see the I2 waiver.
- Golden images are ~25 GB on first pull; the host needs at least twice that plus
  copy-on-write increments.

Declared writable paths and read-only uploads are not supported (`PolicyNotEnforceable` and
`CapabilityNotSupported`). `capabilities().verify_root` is `/workspace/verify` because the macOS
system volume is read-only. A directory uploaded onto an existing directory (such as
`/workspace`) is unpacked into it instead of over it. Not verified on a real host.
