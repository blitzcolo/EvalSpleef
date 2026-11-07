# evalspleef-hostd

The host daemon (`spleef-hostd`) that exposes local sandbox providers (Docker, Tart, Hyper-V, ...)
to the control plane over mutually authenticated gRPC, and `RemoteProvider`, the SPI implementation
on the control-plane side that forwards every call to it. Spec: TECH-DESIGN sections 7.4.10 and 7.5.

## Moving parts

| Module | Role |
| --- | --- |
| `proto/hostd.proto`, `_pb/` | Wire contract and committed generated stubs (`proto/generate.sh` regenerates them) |
| `daemon.py` | `HostDaemon`: providers from entry points, local concurrency cap, TTL guard, drain and maintenance, registration, 15 s heartbeat, certificate rotation; `main()` reads a YAML config |
| `remote.py` | `RemoteProvider`: full SPI over gRPC, typed error translation, streamed exec output |
| `gateway.py`, `hosts.py` | Control-plane side (hostd-gateway) and `HostRegistry`: inventory, slot accounting, states `active`, `draining`, `lost`, `maintenance` |
| `security.py` | CA, one-time registration tokens, CSR signing (30 day certificates), rotation helper |
| `transfer.py` | 4 MiB chunks, sha256 per chunk and per payload, tar framing |
| `conformance.py`, `loopback.py` | `remote-mock` and `remote-docker` conformance targets; a full loopback stack for tests |

## Flow

1. The control plane issues a one-time token for a host id (`RegistrationTokens.issue`).
2. The daemon dials hostd-gateway's registration port (TLS, server authentication only), sends the
   token and a CSR, and receives a 30 day client+server certificate. Its private key never leaves the host.
3. The daemon serves the `Hostd` service on its advertised address (mTLS, only the `control-plane`
   identity is accepted) and heartbeats to the gateway's mTLS port every 15 s. After 60 s of silence
   `HostRegistry.sweep()` marks the host `lost`; listeners fail its trials as environment errors.
4. `RemoteProvider` dials the daemon and forwards SPI calls. A stream that breaks mid-trial raises
   `SandboxLost`; `CapabilityNotSupported`, `PolicyNotEnforceable`, `QuotaExceeded`,
   `SandboxCreateError` and `TransferError` keep their type across the wire.

Drain refuses new creates while existing sandboxes continue. Maintenance refuses every call. The TTL
guard destroys instances past `max_lifetime_s` even while the gateway is unreachable and reports the
reclaimed list on the next successful heartbeat.

## Daemon configuration

```yaml
host_id: mini-01
listen: 0.0.0.0:7445               # port the Hostd service binds
advertise: mini-01.lab.example:7445
service_names: [mini-01.lab.example]   # names the certificate must carry
control:
  registration_address: gateway.example:7443
  gateway_address: gateway.example:7444
  ca_file: ca.pem                  # relative to this file
credentials_dir: credentials       # key.pem (0600) and cert.pem are stored here
registration_token_env: HOSTD_REGISTRATION_TOKEN   # only needed for the first start
providers:
  - name: macos
    adapter: tart                  # entry point in group evalspleef.sandbox_providers
    max_concurrency: 2
    config: {}
```

`HOSTD_REGISTRATION_TOKEN=... spleef-hostd hostd.yaml`. hostd holds no model or vendor secrets: its
secret resolver rejects every lookup.

## Conformance

```bash
uv run pytest --spleef-provider remote-mock --spleef-report /tmp/remote-mock packages/evalspleef-hostd
```

`conformance` extra: `evalspleef-conformance` and `evalspleef-testing` are needed only for
`conformance.py`.

## Verification status

- Verified against real loopback sockets: registration, mTLS (missing, foreign, expired and
  wrong-identity certificates are rejected), heartbeats, chunked transfers, drain, maintenance,
  killed-server behaviour, the 24 applicable conformance cases of `remote-mock`.
- **Not verified on a real host**: every sandbox behind the daemon in the tests is the mock double.
  `remote-docker` is only built when the docker adapter entry point loads and a Docker daemon
  answers; at the time of writing the adapter module does not exist, so the target is reported as
  unavailable (`conformance.UNAVAILABLE`, plus a `RuntimeWarning`) and has never run.
- Multi-machine operation (non-loopback addresses, certificate names for real DNS names, NAT) has not been exercised.

## Known limits

- The control plane reaches the daemon at its advertised address; there is no reverse tunnel, so a
  host behind NAT needs a reachable address. The "daemon connects out" part covers registration,
  heartbeats, certificate renewal and reclaimed reports.
- `HostRegistry` is in memory; a gateway restart forgets hosts until they register again.
- `RemoteProvider.capabilities()` and `isolation_profile()` are synchronous in the SPI. The first
  call blocks for one round trip; `await provider.preflight()` fills the cache without blocking.
- `GRPC_ENABLE_FORK_SUPPORT=0` is set when the package is imported (before gRPC loads), so gRPC's
  fork handlers cannot write into the stderr of sandbox subprocesses.
