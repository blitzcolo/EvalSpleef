# evalspleef-sbx-libvirt

libvirt/QEMU/KVM sandbox provider (`libvirt`) for EvalSpleef: one virtual
machine per trial on a Linux host, driven through the `virsh` CLI and executed
over SSH with `asyncssh`. Implements the sandbox SPI (TECH-DESIGN section 7.4.5)
and ships the isolation profile (FR-SBX-11), Linux and Windows probe sets, and a
conformance target backed by a fake hypervisor.

## Verification status

**Not verified on a real host.** No libvirt daemon, QEMU/KVM domain, real Linux
guest or real Windows guest was started while building or testing this adapter.
What *was* verified:

- the package tests (40) and the conformance target `libvirt-fake` (26 cases
  passed, 0 failed, 7 not applicable) run against fake `virsh` and `qemu-img`
  executables placed on a temporary PATH, plus a **real local asyncssh server**
  (key authentication) acting as the guest. The SSH exec, streaming, timeout
  process-tree kill, stdin, SFTP upload/download and the Linux isolation probes
  therefore run for real against a POSIX process, but not inside a VM.
- Windows support was checked only at the level of PowerShell command
  construction and domain XML (SATA disk, RDP graphics, `x86_64`); the Windows
  exec path, the PowerShell timeout wrapper and the Windows probe set have never
  run on a Windows guest.
- The hypervisor interactions (`virsh define/start/destroy/undefine/list/dumpxml`,
  `qemu-img create -b ... -F qcow2`) are exercised through the fake CLIs, not
  against libvirt.

Treat every hypervisor-specific detail below as designed, not proven.

## Entry points

- `evalspleef.sandbox_providers`: `libvirt = evalspleef_sbx_libvirt.provider:LibvirtProvider`
- `evalspleef.conformance_targets`: `libvirt = evalspleef_sbx_libvirt.conformance:build_targets`

## Model

- **Golden image plus overlay.** `TemplateRef(kind="qcow2", ref=<path>,
  digest=<sha256>)` names the immutable golden qcow2. `create` runs
  `qemu-img create -f qcow2 -F qcow2 -b <golden> <overlay>`; deleting the
  overlay is the reset.
- **Virsh only.** Domains are defined (`virsh define -`), started
  (`virsh start`), listed (`virsh list --all --name` + `virsh dumpxml`),
  destroyed, undefined and reaped through the CLI. No libvirt Python binding is
  used. Labels and the TTL are stored in domain metadata
  (`urn:evalspleef:labels`) and re-read from `dumpxml`.
- **Host-side lifetime guard.** Every domain carries `spleef.created_at`,
  `spleef.expires_at`, `spleef.overlay`, `spleef.template_digest` and
  `spleef.provider`. `list(labels)` filters on the labels from the spec;
  `reap(labels, older_than_s)` destroys matching domains past their age or TTL.
- **Destroy is idempotent**: `virsh destroy`, `virsh undefine` and overlay
  deletion ignore a domain that is already gone.

## Configuration

`LibvirtProvider(instance, config, secrets)` reads:

| key | default | meaning |
| --- | --- | --- |
| `uri` | `qemu:///system` | libvirt connection URI passed to `virsh -c` |
| `virsh_bin` / `qemu_img_bin` | `virsh` / `qemu-img` | executable paths (resolved through PATH when unset) |
| `overlay_dir` | `/var/lib/libvirt/images/spleef` | where per-trial overlays are created |
| `network_off` | `spleef-off` | isolated libvirt network used for `off` |
| `network_full` | `default` | forwarded network used for `full` |
| `network_proxy` / `egress_proxy` | unset | enable `docs_whitelist`/`package_mirror` via the proxy network |
| `nwfilter_off` / `nwfilter_proxy` | `spleef-off` / `spleef-via-proxy` | nwfilters attached to the domain interface |
| `guest_os` | `["linux", "windows"]` | guest OS families this instance may run |
| `arches` | host architecture | guest architectures this instance may run |
| `gui` | `rdp` for windows-only, else `vnc` | declared GUI capability (`none`/`vnc`/`rdp`) |
| `ssh_host` | unset | connect address; unset means discover the guest address with `virsh domifaddr` |
| `ssh_port` | `22` | guest SSH port |
| `ssh_user` | `agent` | SSH account for exec |
| `ssh_key_file` | unset | private key used for SSH (key authentication) |
| `ssh_known_hosts` | unset | host key file; unset disables host key checking (hostd-managed VMs) |
| `ssh_connect_timeout_s` | `10` | SSH connect timeout |
| `startup_timeout_s` | `180` | how long `create` waits for SSH to answer |
| `wait_for_ssh` | `true` | skip the SSH readiness wait (guest-agent only setups) |
| `guest_agent_fallback` | `true` | use `qemu-guest-agent guest-exec` when SSH is unreachable |
| `max_concurrency` | `4` | live domains per provider instance; excess creates raise `QuotaExceeded` |
| `max_lifetime_s` | `7200` | capability ceiling; longer requests raise `PolicyNotEnforceable` |
| `vnc_port` / `rdp_port` | `5900` / `3389` | GUI endpoint port used by `gui_endpoint()` |

## exec and files

- Linux/macOS guests: a string runs through `/bin/sh -lc`; a list is executed
  without a shell. The default working directory is `/workspace`.
- Timeouts run the command under a small shell wrapper: the command is started in
  its own process group when `setsid` exists, and on expiry the wrapper kills the
  descendants first (parent/child walk), then the group and the process. The
  result is `exit_code == 124` with `timed_out=True`; a client-side watchdog
  covers a hung SSH channel.
- `stdout`/`stderr` stream through `on_output` incrementally; output above
  `max_output_bytes` keeps head and tail and sets `truncated=True`.
- Windows guests: PowerShell via OpenSSH with `-NoProfile -NonInteractive
  -Command` semantics (the script travels base64-encoded inside a `-Command`
  invocation); a string is evaluated as PowerShell, a list uses the call
  operator `&`. Timeouts use `Start-Process` plus `taskkill /T /F` and exit 124.
  Not verified on a Windows guest.
- `exec(user=...)` is only valid for the configured SSH account; switching users
  raises `CapabilityNotSupported` instead of silently ignoring the request.
- `upload`/`download` use SFTP. Uploads reject symlinks and special files;
  downloads refuse absolute or escaping symlinks, special files and
  setuid/setgid bits, and enforce `max_bytes` (1 GiB default). Directory modes
  are preserved.

### qemu-guest-agent fallback

When SSH cannot be reached and `guest_agent_fallback` is enabled, `exec` runs the
command through `virsh qemu-agent-command` with `guest-exec` /
`guest-exec-status`. This fallback delivers output **once at the end** (no
streaming), and it cannot honour `cwd`, `env` or `stdin`: those requests raise
`CapabilityNotSupported` rather than being silently dropped.

## Capabilities (declared)

Linux and Windows guests; `x86_64` and `arm64` per configuration; isolation
`L3`; network `off`, `full`, and (when an egress proxy is configured)
`docs_whitelist`/`package_mirror` via `via_proxy`; streaming and stdin; GUI
`vnc`/`rdp` (one protocol per provider instance); no allowlist kinds, no dynamic
network policy, no snapshots, no pause/resume, no GPU, no port expose, no host
mounts; enforced limits: CPU and memory (domain XML). Disk and pids are **not**
enforced.

Network policy mapping: `off` → isolated network + drop-all nwfilter; `full` →
forwarded network; `docs_whitelist`/`package_mirror` → proxy network +
allow-proxy nwfilter, only when `egress_proxy`, `network_proxy` and
`policy.via_proxy` are all present. Any other policy is rejected at create with
`PolicyNotEnforceable`, and an unknown mode with `CapabilityNotSupported`.

## Isolation profile and probes

`isolation_profile.yaml` and `evalspleef_sbx_libvirt.isolation.isolation_profile()`
carry the I1–I16 profile at declared `L3`. The writable-root invariant (I2) is
waived with a reason, a compensating control, an approver and an expiry; the
tests enforce the waiver fields and that the YAML and Python definitions agree.

`probes/linux.sh` and `probes/windows.ps1` are fed to the guest over exec stdin
by `LibvirtProvider.run_probes()`. The Linux set checks non-root, no passwordless
sudo, no default route on `off`, no host mounts, no container socket, CPU and
memory limits, a neutral hostname/environment and credential-shaped values; the
Windows set checks the non-admin token, SMB shares, outbound firewall profiles,
internet reachability and environment leaks. The Windows probe is not verified
on a real guest.

## Conformance

```
uv run pytest -p evalspleef.conformance.plugin \
    --spleef-provider libvirt-fake \
    --spleef-report /tmp/spleef-libvirt-fake-report.json \
    packages/adapters/evalspleef-sbx-libvirt -q
```

`build_targets()` starts fake `virsh`/`qemu-img` executables in a temporary
directory and a real local asyncssh guest (generated host/client keys, an RFB
banner listener for GUI), and returns `libvirt-fake` with `fake=True`, the
`lose_sandbox`, `expire_lifetime` and `fail_create` injectors, and a cleanup
that closes the provider and removes the temporary tree. The package pyproject
disables the autoloaded `spleef-conformance` entry point so the explicitly
loaded plugin does not collide with it.

Not applicable for this target (by capability declaration, not by failure):
C-NET-01 (no observer address for the network canary), C-NET-02 (no egress proxy
configured), C-NET-05 (no dynamic network policy), C-CONC-01 (fake target; the
harness does not model the hypervisor concurrency limit), C-SNAP-01/02 (no
snapshots), C-PORT-01 (no port expose).

## Known limitations

- The golden qcow2 must exist locally; the adapter records the digest but does
  not pull images.
- Guest address discovery relies on `virsh domifaddr --source lease` (a DHCP
  lease) or a configured `ssh_host`.
- Host key checking is off unless `ssh_known_hosts` is set.
- No snapshots, dynamic network changes, port exposure, GPU passthrough or disk
  quotas.
- The guest-agent fallback is non-streaming and limited to the default working
  directory/environment.

Declared writable paths and read-only uploads are not supported (`PolicyNotEnforceable` and
`CapabilityNotSupported`). `capabilities().verify_root` is `/workspace/verify`: a guest's root
directory belongs to root and no template creates `/verify`. The fake guest now resolves every
top-level guest directory for exec as SFTP (chrooted to the guest root) does. Not verified on a
real host.
