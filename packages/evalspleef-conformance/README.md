# evalspleef-conformance

The definition of done for an EvalSpleef adapter (TECH-DESIGN section 15.1). It is a pytest plugin:
one test item is generated per (selected target, case of the target's kind). An adapter is done when
every applicable case passes against its real provider and every not-applicable case names a
reason you accept.

Case ids: sandbox `C-LIFE`, `C-EXEC`, `C-FILE`, `C-NET`, `C-ISO`, `C-CONC`, `C-FAIL`, `C-SNAP`,
`C-GUI`, `C-PORT`; model `M-API`, `M-ID`, `M-LIFE`, `M-ASSIST`, `M-HF`.

## 1. Expose targets

A `ConformanceTarget` is one configured provider instance. The adapter package ships a callable
returning `list[ConformanceTarget]` and registers it in its own `pyproject.toml`:

```toml
[project.entry-points."evalspleef.conformance_targets"]
docker = "evalspleef_sbx_docker.conformance:build_targets"
```

```python
from evalspleef.conformance.target import ConformanceTarget


def build_targets() -> list[ConformanceTarget]:
    return [ConformanceTarget(name="docker-local", kind="sandbox", provider=..., spec_factory=...)]
```

An entry point whose module is not importable yet is skipped with a warning, so adapters under
development do not break the run. Targets can also come from any plugin loaded with `-p` that
implements the hook `pytest_spleef_targets(config) -> list[ConformanceTarget]`.

Fields that matter:

| Field | Meaning |
| --- | --- |
| `name` | Unique; selected with `--spleef-provider NAME`. |
| `kind` | `"sandbox"` or `"model"`; decides which cases run. |
| `provider` | The configured `SandboxProvider` or `ModelEndpointProvider`. |
| `fake` | `True` when the provider talks to a fake of an external system (fake SDK, fake `subprocess`, recorded transport). It is copied into every report row. Never set it `False` for a fake: only a run against the real host counts as "verified on a real host". |
| `spec_factory` | Sandbox only: `(trial_id, *, network, max_lifetime_s) -> SandboxSpec`. Cases open sandboxes through it. |
| `injectors` | Optional async fault injectors, see below. |
| `model_fixture` | Model only: `ModelFixture` with `spec_factory`, `operator_input_factory` (assisted providers), `upstream` (a `FakeOpenAIServer` for scripted replies and error statuses) and `hf_access`. |
| `cleanup` | Optional async teardown, awaited once after the target's cases ran. |
| `observer_hosts` | Sandbox only, optional: async `() -> Sequence[str]` returning every further host name or IPv4 address through which the test machine may be reachable from a sandbox (the gateway of each carrier network, a host alias). See below. |
| `notes` | Free text shown in the report, for example what is faked. |

Network cases also look for an optional `reachable_from_sandbox` attribute: the host name or
address (no scheme, no port) at which a process on the test machine is visible from inside a
sandbox, for example `host.docker.internal`. It is not a dataclass field; add it in a subclass:

```python
@dataclass(frozen=True)
class DockerTarget(ConformanceTarget):
    reachable_from_sandbox: str = "host.docker.internal"
```

Without it C-NET-01, C-NET-02 and C-NET-05 report not-applicable. A provider that enforces no
network policy must therefore never be allowed to hide behind a missing observer: set it.

One address is not enough for C-NET-01: a carrier can map `off` to a host-only network whose
gateway is not the primary observer address, and the host stays reachable through it. C-NET-01
therefore probes `reachable_from_sandbox`, every address `observer_hosts` returns and the test
machine's primary LAN address, and fails if any of them answers. It calls `observer_hosts` after
its sandbox exists, so a network the carrier creates on demand is listed. Targets without the
hook keep working and are probed on the primary and LAN addresses only.

C-FILE-06 uploads to and runs from the verify root of a `role="verifier"` sandbox. The root is
the provider's `capabilities().verify_root` (default `/verify`; a carrier that cannot mount
arbitrary paths names one inside its writable area, such as `/workspace/verify`) unless
`SPLEEF_VERIFY_ROOT` is set, the operator override the control plane also reads. Like the lease
manager, the case declares the root in `SandboxSpec.writable_paths` on carriers with the
`writable_paths` capability; no adapter may mount it because of the role.

Declared paths and upload semantics (module `cases_sandbox_paths`):

| Case | Applies when | Checks |
| --- | --- | --- |
| C-PATH-01 | `writable_paths` declared | two declared paths are empty, writable and executable by `run_as_user`, and a second fresh sandbox sees nothing in them |
| C-PATH-02 | `read_only_upload` declared | after `upload(..., read_only=True)` the tree reads and executes, but overwrite, append, create, delete, rename and chmod all fail as `run_as_user`, and the downloaded tree is byte-identical |
| C-PATH-03 | either capability missing | declared paths raise `PolicyNotEnforceable` at create; `upload(read_only=True)` raises `CapabilityNotSupported` and writes nothing |
| C-FILE-07 | POSIX guest | a directory uploaded onto an existing one merges: a sibling file stays, the same path is replaced |

## 2. Injectors

Fault cases need the adapter to simulate the failure, because only the adapter knows how. An
absent injector makes the dependent case not-applicable; a declared capability whose case cannot
run is a failure.

| Injector | Call | Used by | Effect |
| --- | --- | --- | --- |
| `lose_sandbox` | `await f(sandbox)` | C-FAIL-01 | The vendor reclaims the live sandbox; the next use must raise `SandboxLost`. |
| `fail_create` | `await f()` | C-FAIL-02 | The next `create` raises `SandboxCreateError`; the one after succeeds. |
| `expire_lifetime` | `await f()` | C-LIFE-03 | The sandbox exceeds `max_lifetime_s` (advance a fake clock, or shorten the TTL). |
| `upstream_status` | `await f(http_status)` | M-API-03, M-ASSIST-01 | The upstream answers with that status (401, 404, 422, 429, 5xx, 403). |

`kill_host` (the host running an endpoint vanishes) is documented on the target but no case calls
it yet.

## 3. Run

From the repository root:

```bash
uv run pytest --spleef-provider docker-local --spleef-report conf/docker
uv run pytest --spleef-provider mock-sandbox --spleef-provider mock-model-api   # repeatable
```

* An unknown name fails the run and lists the known targets.
* Without `--spleef-provider` the plugin generates nothing and is inert.
* `SPLEEF_CONFORMANCE_LARGE_FILE_BYTES` lowers the C-FILE-03 payload from 100 MB for slow doubles
  (for example `1048576`); the case itself is unchanged.
* The builtin targets `mock-sandbox`, `mock-model-api`, `mock-model-assisted` and
  `mock-model-external` run against the `evalspleef-testing` doubles. They exist to test the kit
  itself, all are `fake=True`.
* If you load the plugin explicitly with `-p evalspleef.conformance.plugin` while the package is
  installed, pytest also autoloads it through its entry point and refuses with "Plugin already
  registered under a different name". Add `-p no:spleef-conformance` in front.

## 4. Read the report

`--spleef-report PATH` writes `PATH.json` and `PATH.md` (a `.json` or `.md` suffix on `PATH` is
replaced). The terminal also gets one summary line.

```json
{
  "targets": [{"name": "...", "kind": "sandbox", "fake": true, "passed": 24, "failed": 0,
               "not_applicable": 9, "total": 33, "notes": "..."}],
  "results": [{"target": "...", "case_id": "C-NET-01", "title": "...",
               "status": "pass | fail | not-applicable", "detail": "...", "fake": true}],
  "counts": {"pass": 24, "fail": 0, "not-applicable": 9}
}
```

* `pass`: the case ran and held. `detail` is empty.
* `fail`: the case raised; `detail` is the assertion message with the observed values (or
  `ExceptionType: message` for any other exception). A pytest failure is the same case.
* `not-applicable`: `detail` is the reason (capability not declared, injector or observer
  missing, isolation invariant waived). It appears as a pytest skip. Read these: a long list of
  "target supplies no ..." means the adapter exposes less than it could, not that it is
  conformant.
* `fake: true` rows are evidence against a double only.

## 5. Proof that the kit can fail

`tests/test_negative_proofs.py` wraps the mock sandbox into providers with one deliberate defect and
asserts that the matching case fails while an unbroken control passes: exec that ignores timeouts
(C-EXEC-03), a provider that leaks a marker file between sandboxes (C-ISO-01), a non-idempotent
destroy (C-LIFE-02), a mislabelled exit code (C-EXEC-01), a provider that ignores network
`off` (C-NET-01), one that blocks the primary observer but leaves the host reachable on a second
address (C-NET-01), an upload that nests a directory under `/workspace` instead of filling it
(C-FILE-05), a read-only root that ignores the declared verify root (C-FILE-06), a provider
that drops declared paths (C-PATH-01), a read-only upload left writable (C-PATH-02), a carrier
without the capability that accepts declared paths silently (C-PATH-03) and an upload that
replaces a directory instead of merging (C-FILE-07). It also asserts that the builtin targets
fail no case. The mock enforces read-only uploads with the user-immutable file flag, so on
hosts without file flags (Linux) it does not declare `read_only_upload` and the C-PATH-02
proof is skipped with that reason. Run it with
`uv run pytest packages/evalspleef-conformance`.

Everything in this package was verified against the `evalspleef-testing` doubles only; none of it has
run against a real sandbox host or model endpoint.
