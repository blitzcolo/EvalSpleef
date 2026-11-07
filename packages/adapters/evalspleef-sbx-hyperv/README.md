# evalspleef-sbx-hyperv

Hyper-V sandbox provider (`hyperv`) for EvalSpleef: one Generation 2 VM per trial on a
Windows host, created from a differencing VHDX chained to a sha256-pinned golden image,
driven over OpenSSH with [`asyncssh`](https://asyncssh.readthedocs.io). It hosts Windows
guests (the main case, FR-SBX-08) and Linux guests. Implements the sandbox SPI
(TECH-DESIGN section 7.4.6, PRD FR-SBX-08 / US-07) and ships the isolation profile
(FR-SBX-11), the per-OS probe sets and a conformance target.

## Verification status

**Not verified on a real host.** Nothing in this package has run against Hyper-V, against a
Windows guest, or against Windows PowerShell. Every check ran against two doubles that live in
the package:

- `FakeHostShell` (`_fake_host_shell.py`) stands in for the Windows host. It implements the
  `HostShell` protocol, records every PowerShell script the provider sends, and simulates VM
  state on local disk: a "VHDX" is a marker file plus a directory holding the guest
  filesystem, `New-VHD -Differencing` copies the parent directory, `Start-VM` starts a guest
  server. It recognises only the exact script shapes `HyperVProvider` emits.
- `FakeGuestServer` (`_fake_guest.py`) stands in for the guest's OpenSSH endpoint. It is a
  local asyncssh server (SSH exec plus an SFTP server chrooted at the fake VM directory).
  **It executes command lines through `/bin/sh` on the machine running the tests** and
  understands only this small PowerShell-like subset:
  - `powershell -NoProfile -NonInteractive -EncodedCommand <utf-16-le base64>` whose script
    uses `Write-Output [-NoNewline]`, `[Console]::Out.Write` / `[Console]::Error.Write`,
    `Start-Sleep -Seconds|-Milliseconds`, `$env:NAME = '...'`, `Set-Location -LiteralPath`,
    `$var = 'x' * N`, `$pid | Out-File`, `Expand-Archive`, `Compress-Archive`, `Remove-Item`,
    and native calls written as `& 'prog' 'arg' ...` (run as an argv, no shell);
  - `cmd /c` with `exit`, `mkdir`, `rmdir`, `mklink`, `type`, `taskkill /PID`;
  - a bare `taskkill /T /F /PID <n>`; anything else is a plain `/bin/sh -c` line.

  Because anything else is handed to `/bin/sh`, POSIX commands such as `sh`, `cat` and
  `printf` work inside the "Windows" fake although they do not exist on a real Windows guest.
  That is how the kit's POSIX-flavoured cases (C-EXEC-06 marker file, C-SNAP-01/02 marker
  file) run against a Windows spec. They prove the provider's SSH/SFTP plumbing, not Windows
  behaviour.

What the tests do show (112 unit tests, plus 21 conformance cases against the fakes): the
exact PowerShell command sequence the provider issues, spec validation and rejection
(`PolicyNotEnforceable` / `CapabilityNotSupported`, host untouched), quota, create-failure
cleanup, the label registry with `list`/`reap`/TTL sweep, idempotent destroy, checkpoints,
SSH exec with streaming/truncation/stdin/env/cwd, timeout kill (a pid file plus
`taskkill /T /F` on Windows guests, a real `pgrep -P` tree kill on a Linux guest), SFTP and
zip/tar transfer with the I11 sanitisation, the PowerShell Direct command shape, and the
profile/probe artefacts.

Still unverified, and the first things to check on a real host:

- the Hyper-V cmdlets and parameters as written: `New-VHD -Differencing`, `New-VM
  -Generation 2`, `Set-VMProcessor -Count`, `Set-VMMemory -StartupBytes -DynamicMemoryEnabled`,
  `Connect-VMNetworkAdapter -SwitchName`, `Add-VMNetworkAdapterAcl` on an Internal switch,
  `Start-VM`, `Checkpoint-VM`, `Get-VMSnapshot ... .HardDrives[0].Path` and using that disk as
  a differencing parent; the default adapter name `Network Adapter`; that
  `Get-VMNetworkAdapter ... .IPAddresses` yields an IPv4 once Integration Services are up;
- Windows OpenSSH Server behaviour: exec through `cmd.exe` (the default shell, which this
  adapter assumes is left unchanged), `-EncodedCommand`, `CommandLineToArgvW` quoting of list
  commands, exit-code propagation, and the SFTP server mapping `/workspace/...` to
  `C:\workspace\...`;
- `taskkill /T /F` against the `$PID` the command records in `C:/Windows/Temp`, and that a
  non-administrator can write there;
- Windows PowerShell 5.1 `Compress-Archive` / `Expand-Archive` (5.1 writes backslash entry
  names in some builds; the extractor normalises them but this was not exercised);
- PowerShell Direct `New-PSSession -VMName` / `Copy-Item -ToSession|-FromSession -Recurse`;
- `probes/windows.ps1` and `probes/linux.sh` were never executed in a guest; the tests only
  check their text and how they are delivered through `exec`;
- enforcement of the Private / Internal vSwitch and ACL network policy, vCPU and memory
  limits, and the isolation claims in the profile;
- the golden image recipe below, host concurrency, start time and disk headroom.

## Entry points

- `evalspleef.sandbox_providers`: `hyperv = evalspleef_sbx_hyperv.provider:HyperVProvider`
- `evalspleef.conformance_targets`: `hyperv = evalspleef_sbx_hyperv.conformance:build_targets`

## Provider configuration

`HyperVProvider(instance, config, secrets)` reads these keys. The provider runs on the Windows
host (inside hostd) and shells out to PowerShell locally.

| key | default | meaning |
| --- | --- | --- |
| `host_shell` | `PowerShellHostShell` | an injected `HostShell` (tests pass `FakeHostShell`) |
| `powershell_bin` | `powershell.exe` | binary the real host shell spawns with `-NoProfile -NonInteractive -Command` |
| `host_command_timeout_s` | `120` | default budget of one host script |
| `state_dir` | `~/.evalspleef/hyperv` | registry holding labels and the lifetime guard, and snapshot records |
| `vm_dir` | `<state_dir>/vhds` | where differencing VHDXs are created |
| `private_switch` | `spleef-private` | Private vSwitch used for network mode `off` |
| `internal_switch` | `spleef-internal` | Internal vSwitch used for allowlist modes |
| `external_switch` | `spleef-external` | External vSwitch used for `full` |
| `max_concurrency` | `4` | live VMs at a time; excess `create` calls raise `QuotaExceeded` |
| `max_lifetime_s` | `14400` | capability ceiling for `spec.max_lifetime_s` |
| `start_timeout_s` | `300` | budget for the OpenSSH port to answer after `Start-VM` |
| `ssh_port` | `22` | guest SSH port (a guest address of the form `ip:port` overrides it) |
| `ssh_user` | `agent` | the non-administrator account commands run as |
| `ssh_key` / `ssh_password` | unset | `SecretRef`s resolved through the secret store; the key is the control-plane client key |
| `ssh_known_hosts` | unset | passed to asyncssh; unset disables host-key checking, set it in production |
| `ssh_connect_timeout_s` | `15` | SSH login timeout |
| `transfer` | `sftp` | `sftp` or `ps-direct` (no-network fallback) |
| `ps_direct_user` / `ps_direct_password` | `ssh_user` / unset | guest credential for PowerShell Direct; the password is a `SecretRef` |
| `rdp_port` | `3389` | port in the `rdp://` endpoint returned by `gui_endpoint()` |
| `ttl_sweep_interval_s` | `30` | interval of the background TTL sweep |

The three vSwitches are created once by host setup; the provider only connects VMs to them.

## Capabilities and lifecycle

- Windows and Linux guests, x86_64 only (ARM64 hosts need separate verification), isolation
  L3, `max_concurrency` from config, GUI `rdp`, snapshot `memory` (checkpoints, for template
  speed-up only) with fan-out, streaming and stdin, no GPU (GPU-P is not supported),
  `enforces_limits` = cpu and memory, no dynamic network policy, no port exposure.
- `create`: `New-VHD -Differencing -ParentPath <golden.vhdx>` -> `New-VM -Generation 2` ->
  `Set-VMProcessor -Count` -> `Set-VMMemory` -> `Connect-VMNetworkAdapter` (switch by network
  mode) -> `Start-VM` -> poll `Get-VMNetworkAdapter` for an IP and the OpenSSH port. Any
  failure destroys the VM and its disk before `SandboxCreateError` is raised. A spec the
  adapter cannot honour is rejected before the host is touched.
- `destroy`: `Stop-VM -TurnOff`, `Remove-VM -Force`, delete the differencing disk, drop the
  registry entry. Missing pieces count as success, so destroy is idempotent.
- Labels and the lifetime guard live in `<state_dir>/sandboxes/<vm>.json`
  (`spleef.provider`, `spleef.trial`, `spleef.lease`, all `spec.labels`, `created_at`,
  `expires_at`). `list` cross-checks `Get-VM`; `reap` destroys matches that are older than the
  given age or past their TTL; a background sweep destroys expired VMs.
- `snapshot(name)` runs `Checkpoint-VM` and returns a filename-safe id
  `<vm>@<name>`; `create_from_snapshot` builds a new differencing disk from that checkpoint
  disk. Checkpoints are a template speed-up, never a way to reuse a VM that ran a trial (I14).
- The template is `TemplateRef(kind="vhdx", ref=<golden.vhdx path>, digest="sha256:...")`.
  The digest is recorded and reported by `describe()`; verifying the file hash before use is
  hostd's job (see "Golden image").

## Network policy

| mode | vSwitch | extra |
| --- | --- | --- |
| `off` | Private | no uplink at all |
| `full` | External | none |
| `docs_whitelist` / `package_mirror` + `via_proxy` | Internal | `Add-VMNetworkAdapterAcl -RemoteAddress <proxy ip> -Direction Both -Action Allow` |
| `docs_whitelist` / `package_mirror` + `allow_cidrs` | Internal | one ACL entry per IP/CIDR |

`allowlist_kinds = {ipv4, cidr}`: port ACLs take addresses, so a hostname allowlist is only
accepted together with `via_proxy` (the proxy then enforces and logs hostnames, I16). Allowlist
rules on `off`/`full`, an allowlist mode with neither rules nor proxy, a non-literal proxy
address, `via_proxy` together with `allow_cidrs`, and malformed CIDRs are all rejected with
`PolicyNotEnforceable`. `SandboxSpec.env` is rejected at create (`CapabilityNotSupported`):
environment variables are applied per `exec`.

## exec, timeouts and files

- A `str` command runs under the guest's default shell: on Windows
  `powershell -NoProfile -NonInteractive -EncodedCommand <b64>` (the script is encoded, so no
  quoting layer exists); on Linux `sh -lc '<cmd>'`. A `list` command is never given to a
  shell by the adapter: on Windows it is joined with `CommandLineToArgvW` quoting and sent as
  the SSH command line; on Linux it is `shlex`-joined. With `cwd` / `env` / a timeout, a
  Windows list command is wrapped as `Set-Location ...; $env:K = '...'; & 'prog' 'arg'`.
- The OpenSSH `DefaultShell` of the golden image must stay `cmd.exe`: list commands are
  command lines for it, string commands start PowerShell explicitly.
- Commands run as the SSH account (`ssh_user`, must equal `spec.run_as_user`); asking for a
  different `user` raises `CapabilityNotSupported`.
- Output streams incrementally through `on_output`; above `max_output_bytes` it is truncated
  keeping head and tail and flagged `truncated=True`.
- Timeouts: when `timeout_s` is set, the command first records a pid (Windows:
  `$pid | Out-File C:/Windows/Temp/spleef-<id>.pid`; Linux: `echo $$ > /tmp/spleef-<id>.pid`).
  On expiry the adapter reads the pid over a second SSH channel and kills the tree
  (`taskkill /T /F /PID` on Windows; a `pgrep -P` walk with `kill -KILL` on Linux, which needs
  `pgrep`), then drops the channel. The result is `exit_code == 124`, `timed_out=True`.
- Files: `sftp.put` / `sftp.get` for single files. Directories travel as zip
  (`Compress-Archive` / `Expand-Archive`, Windows guests) or tar streams over exec (Linux
  guests, needs `tar`). Remote paths must be absolute (`/workspace/...` or `C:/...`) and may not
  contain `..`. The temporary archive in the guest is removed after the transfer.
- I11 on download: a remote symlink whose target is absolute, a Windows drive path or contains
  `..` raises `TransferError`; archive members that escape the export root, special files,
  setuid bits and symlinks leaving the tree are refused, sizes are capped, `max_bytes` is
  enforced. Uploads refuse symlinks leaving the uploaded tree and special files.
- `transfer: ps-direct` replaces SFTP with PowerShell Direct (`New-PSSession -VMName` +
  `Copy-Item -Recurse -ToSession|-FromSession`), for guests with no usable network. `/workspace`
  paths are translated to `C:\workspace`; downloads are checked afterwards for links and size.
  Caveat: the guest password is embedded in the PowerShell script that runs on the host, so it
  is visible in that process's command line for the duration of the copy.

## Isolation profile and probes

`isolation_profile()` returns I1-I16 with `declared_isolation=L3`; `isolation_profile.yaml`
carries the same content for the audit and the tests keep them in sync. I2 (writable guest
disk) is the only waiver: reason, compensating controls (fresh differencing disk per trial
chained to the pinned golden image, canary scan, disk deleted), approver and expiry
(`2027-03-01`).

- `probes/windows.ps1` checks `whoami /groups` has no Administrators entry, `Get-SmbShare` shows
  no extra (host) share, `Get-NetFirewallProfile` has outbound default `Block`,
  `Test-NetConnection` to an external host fails, `Get-ChildItem Env:` holds no credential or
  evaluation identifier, and a neutral computer name. The firewall and connectivity checks
  apply to network mode `off`; the control plane passes the mode in `SPLEEF_NET_MODE` (default
  `off`) and the script skips those two checks for other modes.
- `probes/linux.sh` is the Linux-guest variant (non-root, no sudo, neutral hostname, no
  default route, clean environment).
- `run_isolation_probes(sandbox, os_family)` delivers them through `exec`: the Windows script as
  one PowerShell command, the Linux script on `/bin/sh -s` stdin.

## Golden image

Recipe notes for the Windows golden VHDX (TECH-DESIGN 7.4.6). None of it is verified here.

1. **Unattended install** with `windows/autounattend.xml` (kept beside the Dockerfile in the
   template tree): partition and install, skip OOBE, create the local user `agent` in the
   `Users` group only (never `Administrators`), set the built-in Administrator account
   disabled or to a random password that is not stored in the image.
2. **OpenSSH Server**: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, `sshd`
   startup automatic, `DefaultShell` left at `cmd.exe`. `sshd_config`: `PubkeyAuthentication
   yes`, `PasswordAuthentication no`, `AllowUsers agent`, `Subsystem sftp sftp-server.exe`.
   Install only the control-plane public key in `C:\Users\agent\.ssh\authorized_keys` and
   restrict its ACL to `agent` and `SYSTEM`. Create `C:\workspace` owned by `agent` and make
   it the SFTP root target for `/workspace`.
3. **Non-admin agent**: no administrator group membership, no UAC elevation path, no stored
   credentials; `tar.exe` is present (Windows 10 1803+), Windows PowerShell 5.1 is the
   default PowerShell.
4. **Firewall**: inbound TCP 22 allowed from the management network, outbound default `Block`
   so the `off` probe holds (return traffic of sshd is stateful).
5. **No telemetry, no updates, no outbound chatter**: `AllowTelemetry=0`, Windows Update and
   Update Orchestrator disabled (`NoAutoUpdate=1`), Defender cloud reporting and sample
   submission off (`Set-MpPreference -MAPSReporting Disabled -SubmitSamplesConsent
   NeverSend`), Delivery Optimization, Store and Edge background tasks off, a neutral
   computer name (no `spleef`/`eval`/`trial`/`batch`).
6. **Seal**: `sysprep /generalize /oobe /shutdown`, then keep the VHDX read-only; it is the
   parent of every differencing disk and must never change.
7. **Record the digest**: `Get-FileHash -Algorithm SHA256 golden.vhdx`; the template builder
   stores `sha256:<hex>` in the template registry as `TemplateRef.digest`, and hostd verifies
   the hash of the file on each Windows host before first use.
8. **Licence**: Windows Server Datacenter / evaluation / VDA is confirmed by the platform
   engineer and written into the host inventory (FR-SBX-08).

## Conformance

```
uv run pytest --spleef-provider hyperv-fake \
    --spleef-report /tmp/spleef-hyperv-fake-report.json \
    packages/adapters/evalspleef-sbx-hyperv -q
```

The conformance kit's pytest plugin is registered automatically through the `pytest11` entry
point of `evalspleef-conformance`, so an extra `-p evalspleef.conformance.plugin` makes pytest
abort with "Plugin already registered under a different name". Drop the `-p`.

The target is `hyperv-fake`, always `fake=True`. It runs the real provider over `FakeHostShell`
and local asyncssh guests, injects `lose_sandbox`, `expire_lifetime` and `fail_create`, and
starts a plain threaded TCP listener as the RDP stub (the kit reads the GUI endpoint after the
sandbox is destroyed, and each case runs in its own event loop).

Result: 21 passed, 0 failed, 12 not-applicable.

Not-applicable cases and why:

- C-NET-01: the target supplies no `reachable_from_sandbox` canary host.
- C-NET-02: the allowlist kinds are `ipv4` and `cidr`, not `hostname` (hostnames need a proxy).
- C-NET-04, C-ISO-02/03/04/05: Linux-only probes; the target's spec is a Windows guest.
- C-NET-05: no dynamic network policy.
- C-ISO-01, C-ISO-06: POSIX probes; the target's spec is a Windows guest.
- C-CONC-01: the kit skips concurrency limits for doubles.
- C-PORT-01: `port_expose` is not declared.

C-SNAP-01/02 pass against the fake because the fake guest runs `sh`/`cat` through the host's
`/bin/sh`; on a real Windows guest the kit's POSIX marker-file commands would not exist.

## Known limitations

- Everything under "Verification status" that is not yet verified on a real host.
- Linux-guest timeouts need `pgrep`; Linux directory transfers need `tar`.
- The pid file lives in `C:/Windows/Temp` (Windows) or `/tmp` (Linux) and is not removed.
- The adapter cannot switch users inside a guest; `run_as_user` is the SSH account.
- Disk size is not enforceable through these cmdlets; `enforces_limits` omits disk.
- `snapshot` is a template speed-up. Restoring "memory" state is the Hyper-V checkpoint's
  behaviour; this adapter only builds a fresh differencing disk from the checkpoint disk.
