# PLAN.md

Build plan and progress tracker for the EvalSpleef platform. The orchestrator updates it at every phase change. Requirements live in `PRD.md`; design lives in `TECH-DESIGN.md`. This file records only what is being built, in what order, by whom, and how it is proven.

Status legend: `todo`, `doing`, `done`, `blocked`.

## Goal

Deliver the whole platform described in `PRD.md` and `TECH-DESIGN.md` as a uv workspace: SPIs, the migrated evaluation core, task packs, gateway, control plane with the `spleef` CLI, runners, host agent, egress proxy, conformance kit, test doubles, every sandbox and model adapter, templates and deployment manifests.

## Decisions taken for this build

| Topic | Decision | Reason |
| --- | --- | --- |
| Language and docs | English for code, comments, AGENTS.md and commit messages | User answer |
| Scope | Full build of all packages | User answer |
| v2 reference code | Migrated from the read-only v2 repository: core (131 unit tests), schemas, 10 task packs, specs | User answer; the source folder will be deleted |
| Commits | Local, batched, on `main`; no push, no remote; commit rules in `AGENTS.md` | User answer |
| LiteLLM | Not a workspace dependency. Its pinned transitive dependencies (uvicorn, python-multipart, websockets, rich) conflict with the rest of the lock, and resolving it stalls the solver. The gateway is a self-contained FastAPI service behind the `GatewayAdmin` contract; a LiteLLM plugin may be added later in its own environment (`uv run --with`) | TECH-DESIGN 6.3 names a self-built gateway as the fallback |
| Inspect AI | Invoked as an external CLI by the inspect runner, not a workspace dependency | Same lock-conflict reason |
| SPI refinements | `ModelEndpointProvider.upstream()` returns the dataclass `UpstreamRoute` instead of a free-form dict. Added `GatewayAdmin`, `HFAccessChecker`, `SecretResolver`, `ModelCapabilities`/`ProbeReport`, `IsolationProfile`, `match_requirements`, `admit_model` to `evalspleef-spi`. Added package `evalspleef-testing` | Keeps vendor shapes out of the contract; gives every lane shared doubles |
| Python | 3.12 (uv-managed); the reference core also passes on newer interpreters | PRD fixes 3.12 |

## Build order

A run is one phase of work. Lanes inside a run have disjoint write scopes and hand data to each other through files.

| Phase | Lanes (write scope) | Needs | Status |
| --- | --- | --- | --- |
| Repository foundation | AGENTS.md, .gitattributes, .gitignore, commit-msg hook, uv workspace, spi, migrated core, schemas, task packs, docs | v2 source | done |
| Foundations run | testing-doubles, core-v3, task-packs-v3, egress-proxy, gateway-protocols, conformance-kit | Foundation | done |
| Engines run | gateway-core, control-data, runners, control-config, sbx-docker, sbx-e2b-family, model-openai-compat, model-novita, hostd-remote, control-endpoints, control-sched, control-trial, control-cli | Foundations run | done |
| Breadth run | sbx-tart, sbx-apple-container, sbx-libvirt, sbx-hyperv, sbx-windows-sandbox, sbx-k8s-agent-sandbox, sbx-device-pool, harbor-bridge, model-vpc-vllm, templates, deploy, docs | Foundations run | done |
| Integration | End-to-end on real surfaces, conformance on local Docker, review | Engines and Breadth runs | done |

## Lane scopes

| Lane | Owns |
| --- | --- |
| testing-doubles | `packages/evalspleef-testing/**` |
| core-v3 | `packages/evalspleef-core/**` (event types, trial versions, batch lifecycle, cost basis) |
| task-packs-v3 | `tasks/**`, `schemas/**`, `scripts/check_repo.py`, `scripts/validate_schemas.py` |
| egress-proxy | `packages/evalspleef-egress/**` |
| gateway-protocols | `packages/evalspleef-gateway/src/evalspleef/gateway/protocols/**` and its tests |
| conformance-kit | `packages/evalspleef-conformance/**` |
| gateway-core | the rest of `packages/evalspleef-gateway/**` |
| control-data | `evalspleef/control/{events,store,ledger,telemetry}` and tests |
| runners | `packages/evalspleef-runners/**` |
| control-config | `evalspleef/control/{config,registry,providers,secrets,taskpack,hf,templates}` and tests |
| sbx-docker | `packages/adapters/evalspleef-sbx-docker/**` |
| sbx-e2b-family | `packages/adapters/evalspleef-sbx-e2b-family/**` |
| model-openai-compat | `packages/adapters/evalspleef-model-openai-compat/**` |
| model-novita | `packages/adapters/evalspleef-model-novita/**` |
| hostd-remote | `packages/evalspleef-hostd/**` |
| control-endpoints | `evalspleef/control/{endpoints,probes,admission}` and tests |
| control-sched | `evalspleef/control/{scheduling,leases,batch}` and tests |
| control-trial | `evalspleef/control/trial` and tests |
| control-cli | `evalspleef/control/cli`, `config/examples/**`, `tests/**` |
| sbx-* (breadth) | one package each under `packages/adapters/` |
| harbor-bridge | `packages/adapters/evalspleef-harbor-bridge/**` (event mapping, Harbor environment bridge, Harbor runner) |
| model-vpc-vllm | `packages/adapters/evalspleef-model-vpc-vllm/**` |
| templates | `templates/**` |
| deploy | `deploy/**` |
| docs | `README.md`, `docs/guides/**` |

## Success criteria

| # | Criterion | Scenario and observable | Evidence |
| --- | --- | --- | --- |
| 1 | Commit hygiene | `python3 scripts/lint_commit_msg.py --history` exits 0. The commit-msg hook rejects `feat: M1 control plane`, a message with a `Co-Authored-By` trailer, and a message with a session link | command output |
| 2 | Static and unit gates | `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run lint-imports` all exit 0; the 131 reference core tests are still present and green | command output |
| 3 | Task packs | `bash tasks/<id>/hidden-tests/run.sh --selftest` passes for every pack that supports it, from the new location | command output |
| 4 | Gateway | Over real HTTP: a trial key completes a chat request through a fake upstream and usage is recorded; an over-budget request gets HTTP 429 with `cost_cap`; a revoked key gets 401; an Anthropic Messages request with a tool round-trips | `curl -i` captures |
| 5 | Assisted endpoint flow | `spleef models register`, `attach`, `probe` against a fake dedicated endpoint ends in state `ready` with a probe report | CLI output |
| 6 | Adapter conformance | `pytest --spleef-provider=<instance>`: real local Docker passes its full subset; the mock sandbox passes; `remote` (hostd over loopback gRPC) in front of Docker passes; every other adapter passes against its transport fake | pytest output |
| 7 | End-to-end batch | With local Docker, the fake model server, the gateway and the control plane: `spleef batch plan/freeze/run/report` on three task packs gives a scripted reference agent a passing tier and an empty agent tier F, with canary scan clean | report markdown and event log |
| 8 | Boundaries stated | The verification table below is accurate | this file |

Stop condition: criteria 1-8 hold with evidence captured, everything is committed, no QA process, container, port or temp directory is left behind.

## Verification table

Every line below comes from a command run during this build, with the exit code read from the tool and not from a pipe. Anything that ran only against a fake of an external system is **not verified on a real host**. A pristine `git clone` of the final tree was installed with `uv sync --frozen --all-packages --all-extras` and checked from there: `pytest` 1725 passed and 1 skipped, and `check_repo.py --run` exit 0 (129 core tests and all ten pack self-tests). That check found a starter file the ignore rules had hidden, which is fixed. Commit counts below were taken before the commit that records them; rerun `python3 scripts/lint_commit_msg.py --history` for the current number.

| # | Result | Evidence | Limits |
| --- | --- | --- | --- |
| 1 | met | `python3 scripts/lint_commit_msg.py --history` exits 0 (88 commits, 0 failing). Running `.githooks/commit-msg` on message files exits 1 for the criterion example `feat: M1 control plane`, for lowercase messages containing `M1`, `P2.1`, `P1` or `W07` (each reported as `matched ...`), for `phase 2`, the word milestone, a `Co-Authored-By` trailer and a session link, and exits 0 for a clean message. `core.hooksPath` is `.githooks` | The hook is client side; `--no-verify` is forbidden by AGENTS.md but not blocked technically |
| 2 | met | `uv run --frozen --no-sync pytest`: 1725 passed, 1 skipped (the e2e test, selected with `-m e2e`), exit 0, 139 s, including the PostgreSQL tests against a real `postgres:16` container. `ruff check .`, `ruff format --check .`, `mypy` (strict, 22 modules, 285 source files) and `lint-imports` (6 contracts kept) all exit 0. `uv lock --check` exits 0. All 131 reference tests are present by name: 129 in `evalspleef-core` (`check_repo.py --run` runs them: Ran 129 tests, OK) and the 2 Harbor mapping tests, which moved with their code to `evalspleef-harbor-bridge/tests/test_events.py` | Run with `--frozen --no-sync`, equivalent to `uv run` because the lock is current |
| 3 | met | `python3 scripts/check_repo.py --run` exits 0: the core unit tests and the self-test of all ten packs on temporary copies | web-001 skips Playwright and runs its static contract only |
| 4 | met | `docs/evidence/gateway-curl.md`: a real `spleef-gateway` process driven with `curl -i`: chat with a trial key and a usage record, HTTP 429 `cost_cap` for an over-budget key, 401 for a revoked key, an Anthropic Messages tool round trip | The upstream is the in-repo fake server; no real vendor traffic |
| 5 | met at command level, with a transport fake for the Novita-hosted call | `packages/evalspleef-control/tests/cli/test_models_assisted.py` (5 tests) drives the real `spleef models register`, `tickets`, `attach` and `list`: register leaves the endpoint `awaiting_operator` with an open ticket; attach to `https://fake-endpoint.novita.ai/v1` sends one `max_tokens=1` request, the probes run, the endpoint ends `ready` with a stored probe report and the ticket closes; non-Novita hosts are refused with no request and no state change; the model is never ready without an attach. Mutation check: ignoring the stored report fails the test. Register and probe against a real gateway process are in transcripts 02 and 03 of `docs/evidence/e2e-batch.md` | The adapter accepts only `https` hosts under `*.novita.ai`, so a loopback endpoint cannot be attached by the real adapter; that guard is deliberate and untouched. Not verified against a real Novita account |
| 6 | met | 21 conformance targets, each run as `pytest --spleef-provider <target>`, all exit 0 with 0 failures; see the next table | Only `local-docker` and `remote-docker` are real. Every adapter fake is **not verified on a real host**. Not-applicable counts follow each target's declared capabilities |
| 7 | met | `docs/evidence/e2e-batch.md`: `pytest tests/e2e -m e2e` exits 0 on local Docker with a real `spleef-gateway` process. 3 packs x 2 models x 2 epochs = 12 trials, 6 scored cells. The scripted reference agent scores Perfect (0 deductions) on all three packs, the empty agent scores Failed (28, 20 and 22 deductions), all 12 canary scans are clean, every sandbox ran on `local-docker`, trial events replay through the core state machine, gateway usage matches the runner's token counts | The model and the judge are fakes; see the bridges below |
| 8 | met | This table. Items that rest on fakes say so | |

### Conformance runs (criterion 6)

| Target | Pass | Fail | Not applicable | Real or fake |
| --- | --- | --- | --- | --- |
| local-docker | 27 | 0 | 6 | real Docker daemon |
| remote-docker (hostd, mutual-TLS gRPC on loopback, in front of the same Docker adapter) | 27 | 0 | 6 | real Docker daemon |
| mock-sandbox | 24 | 0 | 9 | double |
| remote-mock (hostd in front of the mock sandbox) | 24 | 0 | 9 | double behind a real transport |
| tart-fake | 21 | 0 | 12 | fake |
| apple-container-fake | 25 | 0 | 8 | fake |
| libvirt-fake | 26 | 0 | 7 | fake |
| hyperv-fake | 21 | 0 | 12 | fake |
| windows-sandbox-fake | 23 | 0 | 10 | fake |
| k8s-agent-sandbox-fake | 24 | 0 | 9 | fake |
| device-pool-fake | 24 | 0 | 9 | fake |
| perf-host-fake | 20 | 0 | 13 | fake |
| novita-fake (Novita sandbox) | 25 | 0 | 8 | fake |
| e2b-selfhost-fake | 25 | 0 | 8 | fake |
| openai-compat-fake | 3 | 0 | 7 | fake |
| novita-dedicated-fake | 2 | 0 | 8 | fake |
| novita-serverless-fake | 9 | 0 | 1 | fake |
| vpc-vllm-fake | 9 | 0 | 1 | fake |
| mock-model-api, mock-model-assisted, mock-model-external | 9, 2, 4 | 0 | 1, 8, 6 | doubles |

## Verification boundaries

| Surface | Verified how |
| --- | --- |
| Local Docker sandbox (directly and through hostd), gateway process, control plane and `spleef` CLI, PostgreSQL store, task packs, canary injection and scan, grade computation | Run for real on this workstation |
| Unified shell runner (`terminus`) | Run for real in the end-to-end batch, against the fake model server |
| Native in-sandbox shells, Inspect runner | Stub executables only: not verified against the real Claude Code, Codex CLI, OpenCode or Inspect |
| Egress proxy | Package tests only; it is not started in the end-to-end run |
| Docker image templates | logic-001, data-001 and debug-001 built and checked locally; heavy toolchain, macOS and Windows images are not built |
| Compose stack and Helm chart | `docker compose config` validates the compose file; the chart was checked structurally and never rendered by Helm |
| Novita Dedicated and Serverless GPU, Novita Agent Sandbox, Hugging Face, E2B self-host, Tart, Hyper-V, libvirt, apple-container, Windows Sandbox, Kubernetes, device pool | Transport fakes only: **not verified on a real host or account** |

## Known bridges and open follow-ups

These work today and are covered by the evidence above, but they are workarounds or gaps, not finished design.

1. **Gateway public model name.** The gateway prefixes `ep_` to the endpoint id, while the SPI says the model name equals the endpoint id. The command line strips one leading `ep_` when it issues keys (`HttpGatewayAdmin._wire_endpoint_id`), and the gateway tests encode the gateway's convention. One side must change, then the bridge goes.
2. **Isolation preflight probes are prose.** Adapters fill `InvariantEntry.probe` with descriptions, and `run_isolation_probes` runs them verbatim, so the S5 check cannot pass on a real adapter. The command line waives prose probes with a recorded reason (visible in every `preflight.result` event and in the freeze record, and `batch run` prints a warning). The Docker adapter's executable `probes/linux.sh` is never invoked.
3. **Verifier root.** The sandbox root file system is read-only, so the default `/verify` cannot be created; `SPLEEF_VERIFY_ROOT` selects another path (the end-to-end uses `/tmp/verify`).
4. **Observer limit.** When a pack checker aborts early on an invalid answer, other items' violation codes look absent and pass. The trial still fails its hard gates and lands on tier F.
5. **`agent_judge` items.** backend-001, macos-001 and video-001 still contain items no component decides, so those packs pause for a human. debug-001 was fixed (R8 became a script check, R10 and R11 became `llm_judge`).
6. **Duplicated path resolution.** `observe/verifier._locked_local_path` and `trial/prepare._manifest_locked_paths` both resolve hash manifest paths and must be kept in sync.
7. **Route fallback.** The example `providers.yaml` lists the mock sandbox after Docker, and an unpinned run used it for 4 of 12 trials. The end-to-end pins its routes to `local-docker`.
8. **Documentation.** `docs/guides/` was not written. The root `README.md`, 20 package READMEs and the specs under `docs/` stand in.
9. **Hyper-V test double.** The adapter writes a pid file such as `/tmp/spleef-<id>.pid` in the guest for every timed command so the timeout path can kill the process tree. The test double runs guest commands as host processes, so those few-byte files land in the host's `/tmp` and no test removes them. A real guest is destroyed with its pid files.

## Defects found by the checks, and fixed

- A cancelled child kept writing and overwrote other lanes' modules; found by file times and per-file test runs, contained and rewritten against the test contracts.
- The Docker adapter left a created container behind when it failed to start, and `close()` raised across event loops; both found on real Docker and covered by regression tests.
- Uploading a directory as `/workspace` made tar rewrite the root-owned mount point and exit 1. The conformance cases only ever uploaded into `/workspace`, so only the end-to-end found it.
- The conformance plugin exited 5 on a fully passing run, aborted the report when a target cleanup raised, and let one broken adapter break unrelated runs. The hostd Docker target used an image without the agent user.
- The `.gitignore` credential rule also hid the `secrets` source package.
- Canary tokens were written into hash-locked inputs, which broke the pack's own integrity gate. A shortcut that waived that gate was rejected; placements now skip locked and protected paths.
- Six hidden-test scripts used the BSD-only `mktemp -t` and failed inside the Linux image.
- Rubric items that could never produce a pass signal made every trial pause for a human; fixed in the observer and in three pack files, without weakening the rule that an undecidable item goes to a human.
- The batch driver left the `grade.rule_id` column empty.
- The two PostgreSQL test fixtures (control store and gateway store) removed their container without the volume flag, leaving one anonymous Docker volume per run (40 found by the final cleanup inventory). Measured for each fixture: one more volume without the flag, none with it.
- Test runs left empty mock-sandbox directories in the temporary directory (218 found; the hostd and lease suites drop sandboxes without destroying them, about 7 per full run). The mock now removes its root when the sandbox object is dropped or the interpreter exits. The adapter test doubles (Kubernetes, libvirt, Apple container, Tart, Windows Sandbox, Hyper-V) left about 750 more, so a root `conftest.py` now sends every temporary file of a pytest session to one scratch directory that is removed at the end. The number of session-style entries in the temporary directory is the same before and after a full run (975 and 975).
- The example config used one epoch per cell while the spec needs two valid epochs to rank a cell.
- The ignore rule for log files also hid `tasks/data-001/workspace/access.log`, a starter input of the pack that was never committed, so a fresh clone lacked it. Found by running the pack self-tests from a pristine clone.
