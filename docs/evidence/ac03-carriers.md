# AC-03: the same Linux packs on the docker and apple-container carriers

Captured on 2026-11-09 on one Mac (macOS 27.2, Apple M6, 24 GiB) by `tests/e2e/test_carrier_parity.py`.

## Status

**AC-03 passes on the repository.** The first capture of this scenario failed on the apple-container side because of two defects in the apple-container adapter (see "Adapter defects" below); both are fixed in the adapter with regression tests, and the scenario then passed on the repository itself (`SPLEEF_E2E_APPLE_CONTAINER=1 pytest tests/e2e/test_carrier_parity.py -m e2e`: `1 passed in 54.92s`; the whole e2e directory with the opt-in: `5 passed in 100.61s`).

The tables below were captured in a what-if run with a scratch copy of the adapter patched with the first proposed fix (`1 passed in 49.59s`); the run on the repository asserts the same equalities (identical tier and deductions per pack and model on both carriers, clean canary scans, per-carrier environment records) and passed after the adapter fix. The final adapter fix differs from the proposed patch below: the directory is packed without its own top entry and unpacked into the target, which does not depend on the guest's `tar --strip-components`. Gitignored transcripts: `.omo/reports/e2e-ac03/` (latest run) and `.omo/reports/e2e-ac03-whatif/` (the what-if run).

## How to run

```bash
# once: Docker pack images (scripts/build_templates.py rewrites templates/registry.json)
.venv/bin/python scripts/build_templates.py --env logic-001 --env data-001 --env debug-001
# needs Apple's container CLI with its service running and the default kernel installed
SPLEEF_E2E_APPLE_CONTAINER=1 .venv/bin/pytest tests/e2e/test_carrier_parity.py -m e2e -q -s
```

The test is gated by the `e2e` marker **and** by `SPLEEF_E2E_APPLE_CONTAINER=1`. Without the variable it is skipped, so a plain `pytest -m e2e` on another machine is unchanged (`4 passed, 1 skipped`). With the variable set, a missing `container` CLI or a stopped service fails the test instead of skipping it. Transcripts, event logs, batch reports and `summary.json` are written under `.omo/reports/e2e-ac03/{docker,apple-container}/`.

## What the scenario does

1. **Pins the images.** For each pack it takes Docker's `llm-eval/<pack>:local` and records its image id. In this Docker setup (containerd image store) the image id is the OCI index digest. It tags the image `spleef-ac03-<pack>:local` and exports it with `docker save`, which writes an OCI layout with the full index. It then loads that archive into the Apple image store with `container image load`.
   - The Apple store reports the same index digest. The test asserts the template digest of every provisioned sandbox, and the summary records both digests.
   - The Apple store resolves `ref@digest` only through a stored digest reference, and a loaded archive does not create one. So the test also adds `container image tag spleef-ac03-<pack>:local spleef-ac03-<pack>@<digest>`. Without it, the CLI tries to fetch the digest from Docker Hub and gets 401.
2. **Writes one template registry for both runs** (`tag` = `spleef-ac03-<pack>:local`, `image_id` = the digest) and passes it with `--templates-registry`. The registry is the existing mechanism that `AppContext.template_for` reads, so control has no Apple-specific path. Both batches freeze the same template ref and digest.
3. **Runs the same batch on each carrier.** Each carrier gets the example `batch.yaml` and `models.yaml` unchanged, its own `spleef-gateway` process, its own SQLite store and its own JSONL event log, and the full `spleef config validate / models register / models probe / batch plan / batch freeze / batch run / batch report --json --release` sequence. Only `providers.yaml` differs:
   - docker uses the example file unchanged (`local-docker`).
   - apple-container replaces the sandbox instances and routes with one instance, `local-apple-container`: adapter `apple-container`, `name_prefix: spleef-ac03-`, `limits.max_concurrency: 3`.
4. **Checks each run:**
   - no trial is invalidated;
   - `batch run` reports `event projection: 12 trial row(s) checked`, meaning `verify_projection` derived every trial row from the event log;
   - the reference agent scores tier P on every trial and cell, and the empty agent scores F with rule `G-BUDGET-F`/`G-ABORT-04`;
   - 12 canary scans pass with no violations;
   - 24 `sandbox.provisioned` events name the run's instance and the pinned digest, and record a guest kernel measured inside the sandbox;
   - the event log replays through the core trial state machine;
   - gateway usage matches the runner's token counts;
   - every trial row's `versions.sandbox_adapter` and `execution_environment` (provider runtime, provider guest kernel, agent and verifier sandboxes measured on this instance) match facts read outside the platform;
   - the release package agrees: drill-down adapter and declared isolation, frozen provider environment, isolation section;
   - no sandbox of the run is left on its carrier.

   The outside facts come from these sources:
   - docker: the runtime from `docker version`, the guest kernel from `docker info`;
   - apple-container: the runtime from `container --version`, the guest kernel from `uname -r` in a VM the test boots itself, named `spleef-ac03-kernel-*`, from a pack image.
5. **Compares the carriers:**
   - the runtime names and guest kernels differ;
   - the host records are equal;
   - every trial's grade signature is identical: tier, decision rule, penalty, penalty breakdown, failed hard gates and defect summary. Timings are not compared;
   - every cell's status and tier are identical.

## Result (what-if run with the adapter patch)

| pack | model | docker tier / rule / penalty | apple-container tier / rule / penalty | failed hard gates | same |
| --- | --- | --- | --- | --- | --- |
| data-001 | fake-ref-agent (e1, e2) | P / G-F0-PERFECT / 0 | P / G-F0-PERFECT / 0 | none | yes |
| data-001 | fake-empty-agent (e1, e2) | F / G-BUDGET-F / 28 (8 deductions) | F / G-BUDGET-F / 28 (8 deductions) | HG1, HG2, HG3 | yes |
| debug-001 | fake-ref-agent (e1, e2) | P / G-F0-PERFECT / 0 | P / G-F0-PERFECT / 0 | none | yes |
| debug-001 | fake-empty-agent (e1, e2) | F / G-BUDGET-F / 20 (8 deductions) | F / G-BUDGET-F / 20 (8 deductions) | HG1 | yes |
| logic-001 | fake-ref-agent (e1, e2) | P / G-F0-PERFECT / 0 | P / G-F0-PERFECT / 0 | none | yes |
| logic-001 | fake-empty-agent (e1, e2) | F / G-BUDGET-F / 22 (6 deductions) | F / G-BUDGET-F / 22 (6 deductions) | HG1, HG2 | yes |

Every row stands for both epochs, and the two epochs are identical. All 6 cells are `scored` on both carriers. Each row's carrier and guest kernel:

| | docker | apple-container |
| --- | --- | --- |
| sandbox instance / adapter | `local-docker` / `docker` | `local-apple-container` / `apple-container` |
| declared isolation | L1 | L3 |
| guest kernel (every agent and verifier sandbox, measured) | `7.0.14-linuxkit` | `6.18.35` (equal to `uname -r` in a VM booted outside the platform) |
| preflight I1-I9 | 12 of 12 passed, nothing waived | 12 of 12 passed, I7 waived (declared waiver: no pids limit, no disk quota; expires 2027-06-30) |
| canary scans | 12 passed, 0 tokens seen, 0 violations | 12 passed, 0 tokens seen, 0 violations |
| `batch run` wall time | 6.1 s | 29.6 s (concurrency 3) |
| whole carrier leg (validate through report) | 10.3 s | 34.1 s |

The batch report printed by `spleef batch run` on apple-container matches Docker's: `data-001 28/Failed | 0/Perfect`, `debug-001 20/Failed | 0/Perfect`, `logic-001 22/Failed | 0/Perfect`, `event projection: 12 trial row(s) checked`.

## Recorded execution environments

Provider record (`trial.versions.execution_environment.provider`, the same on all 12 rows of each run):

| field | docker | apple-container |
| --- | --- | --- |
| runtime | `docker 29.9.3` | `container 1.6.1` |
| guest | linux arm64, kernel `7.0.14-linuxkit`, 12 CPUs, 8319176704 bytes, `Docker Desktop` | linux arm64, kernel `6.18.35`; CPUs, memory and distribution unreported (they depend on the image and the sandbox spec) |
| host | macos 27.2, kernel 27.2.0, arm64, Apple M6, Mac18,5, 12 CPUs, 25769803776 bytes | identical |
| extra | Docker Desktop 4.99.0 (245822), API 1.56, containerd v2.4.2, runc 1.5.2, docker-init 0.19.0, OCI runtime runc | apiserver 1.6.1 (release), default kernel image `vmlinux-6.18.35-197-debug` (sha256 `fb2cfb79eb1ae19447a85d75682d7fa5cfec97e24beb2609a492b806e8072c8d`), vminit `ghcr.io/apple/containerization/vminit:0.53.0`, install root `/opt/homebrew/Cellar/container/1.6.1/` |

Sandbox records (agent and verifier, measured inside each sandbox):

| | docker | apple-container |
| --- | --- | --- |
| kernel | `7.0.14-linuxkit` | `6.18.35` |
| OS | Debian GNU/Linux 13 (trixie) | Debian GNU/Linux 13 (trixie) |
| CPUs seen | 12 (the shared Docker VM) | 3 (2 requested plus one vCPU of VM overhead) |
| memory seen | 8319176704 bytes (the shared Docker VM) | 2204610560 bytes (a VM sized for the 2048 MiB request) |

## Image identities

| pack | Docker image id (OCI index digest) | Apple store digest after `container image load` |
| --- | --- | --- |
| logic-001 | `sha256:f9810e4fb2dcd0956144823c39dac97525b0f2cdd3ce4eeaa7e9371f8a7f5781` | `sha256:f9810e4fb2dcd0956144823c39dac97525b0f2cdd3ce4eeaa7e9371f8a7f5781` |
| data-001 | `sha256:dcbce79708bac6e27e7a68550c91de3447e62549baa0b3701bc4052b91d8aba5` | `sha256:dcbce79708bac6e27e7a68550c91de3447e62549baa0b3701bc4052b91d8aba5` |
| debug-001 | `sha256:94bed7255a00652aaf4497790bf78a20d72a47361218866324535a559cbb752b` | `sha256:94bed7255a00652aaf4497790bf78a20d72a47361218866324535a559cbb752b` |

The digests are identical because the archive carries the index unchanged. Exporting with `docker save --platform linux/arm64` instead writes only the platform manifest, and the Apple store then wraps it in a new index with a different digest (observed: `480a87e5…` for logic-001). The test therefore saves the full index.

**The cross-carrier template fingerprint check is not built yet** (the D10 remainder in PLAN.md). Nothing in the platform checks that the image an Apple sandbox booted matches the one Docker booted. Equal digests are a property of how this test moves the images, not something the platform guarantees. What the scenario compares is pack behaviour on each carrier.

## Mutation proof

Both mutations ran against the final test file. After each, the file was restored with `cp` and checked with `cmp` (byte-identical).

1. **Apple batch routed to Docker.** In `_apple_providers_yaml`, `"adapter": "apple-container"` became `"adapter": "docker"`. Every trial ran and graded on Docker, and the test failed on the carrier assertion: `assert entry["sandbox_adapter"] == carrier.adapter`, which reported `'docker' == 'apple-container'` (exit 1, `1 failed in 28.69s`).
2. **The Apple carrier records Docker's environment.** A scratch copy of the patched adapter returned `RuntimeFacts(name="docker", version="29.9.3")` and guest kernel `7.0.14-linuxkit` from `environment()`. The test failed on the environment assertion: `assert frozen_env["runtime"] == dict(carrier.runtime)`, which reported `{'name': 'docker', 'version': '29.9.3'} != {'name': 'container', 'version': '1.6.1'}` (exit 1, `1 failed in 51.33s`).

## Adapter defects found by the Apple run (fixed afterwards)

### 1. Uploading a directory onto the `/workspace` mount point fails

Every trial was invalidated as `failed_infra`, and the driver retried each one three times (48 rows), with this error:

```
TransferError: upload to '/workspace' failed (exit 2): tar: workspace: Cannot utime: Operation not permitted
tar: workspace: Cannot change mode to rwxr-xr-t: Operation not permitted
```

**Cause.**
- The adapter mounts `/workspace` as tmpfs. The mount point is owned by root with mode 1777.
- `upload()` packs the local directory with top entry `workspace` and unpacks it with `tar -x -C /` as the non-root sandbox user.
- GNU tar extracts the files, then tries to set the mode and mtime of the existing directory, which that user does not own. It exits 2.
- `--no-overwrite-dir` does not help: tar still tries to chmod, to `rwxrwxrwx`.
- The live conformance run did not catch this because it never uploads onto a mount point.

**Reproduction** (no platform involved):

```bash
container run --rm --name spleef-ac03-repro --read-only --tmpfs /tmp --tmpfs /workspace \
  --user nobody --cap-drop ALL docker.io/library/python:3.12-slim sh -c '
  mkdir -p /tmp/s/workspace && echo hi > /tmp/s/workspace/a && cd /tmp/s && tar -cf /tmp/a.tar workspace
  (umask 000 && tar -xf /tmp/a.tar -C /); echo rc=$?'          # rc=2, same two tar errors
# unpacking into the target instead succeeds: tar -xf /tmp/a.tar -C /workspace --strip-components=1  -> rc=0
```

### 2. Verifier sandboxes have no writable `/verify`

With defect 1 patched, all 12 trials got through the agent phase. Each then failed verification and was retried, again 48 rows:

```
TransferError: upload to '/verify/hidden-tests' failed (exit 1): mkdir: cannot create directory '/verify': Read-only file system
```

- The evaluator's default verify root is `/verify` (`evaluator.py`; `context.py` reads `SPLEEF_VERIFY_ROOT`, default `/verify`).
- The docker adapter mounts a `/verify` tmpfs in verifier sandboxes (`provider.py`: `tmpfs["/verify"]`).
- The apple-container adapter's read-only root has no `/verify` mount.

The test does **not** set `SPLEEF_VERIFY_ROOT` to work around this. That variable applies to the whole process rather than to one carrier, and it would hide the gap between the two adapters.

### The patch as first proposed (superseded by the adapter fix described in Status)

```diff
--- a/packages/adapters/evalspleef-sbx-apple-container/src/evalspleef_sbx_apple_container/sandbox.py
+++ b/packages/adapters/evalspleef-sbx-apple-container/src/evalspleef_sbx_apple_container/sandbox.py
@@ -53,6 +53,9 @@
 # No "--" before the negative pid: dash's kill builtin rejects it ("Illegal number: -").
 _KILL_GROUP = 'pid="$(cat "$1" 2>/dev/null)"; [ -n "$pid" ] && kill -KILL "-$pid"; rm -f "$1"'
 _UNPACK = 'mkdir -p "$1" && umask 000 && exec tar -xf - -C "$1"'
+# A directory unpacks into the target itself: tar never touches the metadata of an
+# existing target such as a root-owned tmpfs mount point.
+_UNPACK_INTO = 'mkdir -p "$1" && umask 000 && exec tar -xf - -C "$1" --strip-components=1'
 
 
 class _CappedBuffer:
@@ -256,8 +259,9 @@
             raise TransferError(f"remote path must name a file or directory, got {remote!r}")
         parent = posixpath.dirname(str(remote_path)) or "/"
         archive = await asyncio.to_thread(build_upload_tar, local, arcname=remote_path.name)
+        is_dir = local.is_dir() and not local.is_symlink()
         result = await self.exec(
-            ["sh", "-c", _UNPACK, "sh", parent],
+            ["sh", "-c", _UNPACK_INTO if is_dir else _UNPACK, "sh", str(remote_path) if is_dir else parent],
             stdin=archive,
             timeout_s=self._provider.transfer_timeout_s,
             max_output_bytes=1 << 20,
--- a/packages/adapters/evalspleef-sbx-apple-container/src/evalspleef_sbx_apple_container/provider.py
+++ b/packages/adapters/evalspleef-sbx-apple-container/src/evalspleef_sbx_apple_container/provider.py
@@ -469,6 +469,11 @@
             "/workspace",
             "--tmpfs",
             f"/home/{user}",
+        ]
+        if spec.role == "verifier":
+            # Hidden tests are uploaded to and run from /verify (the evaluator's default root).
+            args += ["--tmpfs", "/verify"]
+        args += [
             "--cap-drop",
             "ALL",
             "--init",
```

After the patch, the adapter's README and isolation profile (I2/I3 list the writable paths) should mention `/verify` for verifier sandboxes, and the adapter's fake CLI tests should cover a directory upload onto an existing mount point.

## Contract questions

1. **Retagging OCI templates per carrier.**
   - `AppContext.carrier_template` maps a template per instance only for `vendor` in {`novita`, `e2b`}, and it keeps the OCI digest. A carrier with its own OCI store (apple-container) cannot be given a different `oci` ref through the registry's `templates` section.
   - This test therefore uses a per-run registry file through `--templates-registry`.
   - Should the registry allow an `oci` retag per instance? And who creates the Apple-store image and its digest reference: the Template Builder (`scripts/build_templates.py` has no Apple target) or the adapter?
2. **Whether the digest reference is the adapter's job.** The adapter runs `ref@digest`. The Apple store resolves that only through a stored `repo@digest` reference, which `container image load` does not create. Should the adapter (or the builder) create it, or should images always come from a registry?
3. **`spleef batch run` exits 0 when every trial is invalidated.** In the failing run, all 48 rows were `failed_infra`/`invalid` and all 6 cells were `Pending`, but `batch run` still exited 0. The scenario catches this by asserting that there are no `trial.invalidated` events. Should a batch with no scored cell exit non-zero?
4. **Container names on the Docker side.** The docker adapter has no name option, so its sandboxes cannot carry the `spleef-ac03-` prefix. The test identifies and checks them by the sandbox ids in its own event log.

## What is not proven

- **AC-03 on the repository as committed.** The apple-container leg fails until the adapter patch above lands. The passing run used a scratch copy of the patched adapter.
- **A real model.** Both agents and the judge are the in-repo fake OpenAI-compatible server, scripted with reference solution A and with no tool calls respectively.
- **Byte-identical images enforced by the platform.** See the fingerprint note above (D10). The template registry still lists the Docker digest, and the docker adapter falls back to the bare tag when `ref@digest` does not inspect.
- **Isolation.**
  - The Apple carrier's I7 is waived by the adapter's declared waiver: no pids limit, no disk quota.
  - The `spleef-ac03-` name prefix the lab rules require is also the guest hostname. That leaks the platform name into the guest. The preflight I8 probe checks the environment only and passed. The adapter's `probes/linux.sh` neutral-hostname check would flag it. Use the neutral default prefix for a real-model run.
- **Network-on packs and whitelist modes.** All three packs run with `network: off`.
- **Clock skew.** The Apple guest clock trails the host by up to about 0.4 s. tar warned "time stamp … in the future" on uploads. This did not affect the outcome.
- **Timing and scale.** One run each at concurrency 3 (Apple) and the example's 8 (Docker). Wall times are single samples.
