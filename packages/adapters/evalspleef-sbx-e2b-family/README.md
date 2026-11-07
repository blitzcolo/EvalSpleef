# evalspleef-sbx-e2b-family

Sandbox providers for the E2B family of sandbox platforms (TECH-DESIGN section 7.4.1):

| Entry point (`evalspleef.sandbox_providers`) | Class | SDK |
| --- | --- | --- |
| `novita-sandbox` | `evalspleef_sbx_e2b_family.novita:NovitaSandboxProvider` | `novita-sandbox` 2.x (Novita Agent Sandbox) |
| `e2b-selfhost` | `evalspleef_sbx_e2b_family.selfhost:E2BSelfHostProvider` | `e2b` (self-hosted E2B infra, `E2B_DOMAIN`) |

Conformance targets (`evalspleef.conformance_targets`):
`evalspleef_sbx_e2b_family.conformance:build_targets` -> `novita-fake`, `e2b-selfhost-fake`.

## Status: not verified against a real Novita account or E2B cluster

Everything in this package was written against the installed SDK sources and tested
against an in-memory fake of the SDK surface (`fakes/`). **No call has ever been made to
the Novita API or to an E2B cluster.** In particular these are unverified until a real-host
run: the actual behaviour of `commands.run` / `files.*` against envd, the `allow_out` /
`deny_out` semantics, the vendor TTL behaviour, the declared isolation level (L3,
`verified_isolation: null`), the Novita quota numbers and the template builds. Conformance
reports for the fake targets carry `fake=True` for this reason.

## Layout

```
src/evalspleef_sbx_e2b_family/
  base.py             E2BFamilyProvider / E2BFamilySandbox: all shared SPI logic
  novita.py           NovitaSandboxProvider + the novita isolation profile
  selfhost.py         E2BSelfHostProvider (E2B_DOMAIN) + its isolation profile
  template_builder.py OCI image (by digest) -> vendor template, JSON digest registry
  conformance.py      build_targets(): novita-fake / e2b-selfhost-fake with injectors
  fakes/              in-memory fake of the SDK surface the adapter calls
isolation_profile.yaml  reviewed isolation profile of novita-sandbox (TECH-DESIGN 7.7)
tests/                unit tests, fake-signature parity tests, conformance-target tests
```

## What the base class does

* **Lifetime.** The instance `timeout` is `spec.max_lifetime_s + 300`. The SDK default of
  5 minutes is never used. `on_timeout=pause` / `auto_resume` are never set, so a sandbox
  cannot be paused and reused across trials.
* **exec.** `Sandbox.exec` maps to `commands.run(background=True)` followed by
  `handle.wait(...)` (the SDK raises on a non-zero exit, the adapter turns that into an
  `ExecResult`). `cwd`, `user`, stdout/stderr callbacks and stdin are supported; output over
  `max_output_bytes` is truncated keeping head and tail. On `timeout_s` expiry the
  process group inside the sandbox is killed (`kill -KILL -<pgid>`, pgid from `ps`), the
  result is `timed_out=True`, `exit_code=124`.
* **Environment sanitization (invariant I8).** The platform may inject identifying
  variables. Every command therefore runs through
  `env -i <spec.env + exec env> PATH=... TERM=dumb HOME=/home/<user> /bin/bash -lc '<cmd>'`.
  Variables baked into the template ENV are *not* inherited by `exec`; pass what a task
  needs via `SandboxSpec.env` / `exec(env=...)`. Cleaning the agent user's login profile is
  a template-build concern (the inner login shell still sources it).
* **Files.** `upload` / `download` use `files.write` / `files.read`. Directories are packed
  as tar, moved through the files API and unpacked / read back inside the sandbox. Only
  paths under `/workspace` are accepted; downloads follow symlinks inside the sandbox and
  refuse any that resolve outside `/workspace` (`TransferError`); uploads refuse local
  trees containing symlinks that leave the tree. File and tar payloads travel as bytes;
  command stdout is `str` in the SDKs, so do not use `exec` to move binary data.
* **Network.** `off` -> `allow_internet_access=False`. Allowlist modes ->
  `network={"allow_out": hosts + cidrs}` (hostnames, wildcards, IPs, CIDRs). With
  `via_proxy`, only the proxy host is allowed (`allow_out=[proxy_host]`, ADR-v3-07). An
  allowlist mode without hosts or proxy raises `PolicyNotEnforceable`. Runtime changes use
  `set_network` (novita) / `update_network` (e2b).
* **Quotas and capability limits** come from `config["limits"]`
  (`max_concurrency`, `max_lifetime_s`, `max_resources`) on top of vendor defaults
  (novita paid tier: 100 concurrent, 3 h, 8 vCPU / 8 GiB / 20 GB). Over-concurrency raises
  `QuotaExceeded`; requests beyond the ceiling raise `CapabilityNotSupported`.
* **Labels.** `spec.labels` are written as sandbox `metadata`; `list(labels)` and
  `reap(labels, older_than_s)` filter on them. `destroy()` is `kill` and is idempotent.
* **Errors.** Create failures -> `SandboxCreateError`, vendor rate limit -> `QuotaExceeded`,
  a vanished sandbox -> `SandboxLost`.
* **Secrets.** The API key is resolved through the `SecretResolver` at the moment of use and
  only passed to the vendor control API; it is never put in the sandbox environment,
  metadata or template (FR-ISO-02).

The base class needs `bash`, `ps`, `tr`, `tar`, `readlink`, `dirname` in the template image.

## Configuration

```yaml
sandbox_instances:
  novita-us-phx-1:
    adapter: novita-sandbox
    config:
      region: us-phx-1            # default; sandbox domain <region>.sandbox.novita.ai
      api_key_ref: vault://novita/main
      limits: {max_concurrency: 80}
  e2b-vpc:
    adapter: e2b-selfhost
    config:
      domain: e2b.internal.example   # or the E2B_DOMAIN environment variable
      api_key_ref: env://E2B_API_KEY
```

## Template builder

```python
from evalspleef_sbx_e2b_family.template_builder import TemplateBuilder, TemplateRegistry

registry = TemplateRegistry(Path("template_registry.json"))
builder = TemplateBuilder(
    registry,
    vendor="novita",
    provider="novita-sandbox",
    client_kwargs={"api_key": key, "domain": "us-phx-1.sandbox.novita.ai"},
)
build = builder.build_from_oci_image(
    "registry/img@sha256:...", name="web-001", cpus=4, memory_mb=8192
)
build.template_id
```

The image reference must pin a digest. The build removes sudo as root, then
`set_user("agent")` (the image must provide that user), and fixes CPU / memory on the
template. The `(oci digest -> provider -> template id)` mapping is written to the JSON
registry. The post-build `env-fingerprint` consistency check (TECH-DESIGN 7.6) is not part of
this package.

## Fakes and tests

`fakes/` replaces the SDK entry points the adapter modules imported (`Novita`, `Sandbox`,
the exception classes) via `patch_adapter_module`. Commands run as real local subprocesses
inside a per-sandbox temp directory in which `/workspace` exists as a normal directory.
Deliberate differences from the real thing: no isolation; network policy is modelled by
pointing `http_proxy` and friends at a closed loopback port; commands run under
`/bin/bash -c` rather than the envd login shell; output callbacks fire once per stream when
the command ends. `tests/test_fake_signatures.py` compares the fake's parameter names with
the installed SDK classes so an SDK rename fails loudly.

```
uv run --frozen --no-sync pytest packages/adapters/evalspleef-sbx-e2b-family -q
uv run --frozen --no-sync pytest --spleef-provider novita-fake \
    --spleef-report /tmp/spleef-novita-fake-report.json packages/adapters/evalspleef-sbx-e2b-family
```

The fake conformance targets declare `enforces_limits=frozenset()` and no `max_concurrency`
(the fake does not model them), so C-ISO-05 and C-CONC-01 report not-applicable for the
doubles; C-NET-01/02/05 are not-applicable because the double supplies no
`reachable_from_sandbox` host. A real-host run must not use these adjustments.

## Isolation profile

`isolation_profile.yaml` mirrors `NovitaSandboxProvider.isolation_profile()` (a test pins
the two). Declared level L3, `verified_isolation: null` until the conformance C-ISO-* subset
passes on a real host. I2 is waived (single-use VM) with compensating controls and an
expiry of 2027-03-01, approver `eval-lead` - both are placeholders for the real evaluation
owner to confirm.

## Known limitations

* `novita-sandbox` ships no `py.typed`; the two modules importing it (`novita.py`,
  `template_builder.py`) carry `# mypy: disable-error-code=import-untyped`. A root mypy override
  (`ignore_missing_imports` for `novita_sandbox.*`) would let them go.
* `e2b` is an optional extra in `pyproject.toml` (`selfhost`) but `selfhost.py` and
  `conformance.py` import it unconditionally, so the extra is effectively required.
* Snapshots are declared `memory` with fan-out, taken from the vendor docs; the restore path
  has only been exercised against the fake.
