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
| Integration | End-to-end on real surfaces, conformance on local Docker, review | Engines and Breadth runs | done against the eight success criteria below; the spec review further down lists what the PRD still needs |

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
| novita-live (Novita sandbox, real account, 2026-11-08) | 25 | 0 | 8 | real Novita Agent Sandbox |
| e2b-selfhost-fake | 25 | 0 | 8 | fake |
| openai-compat-fake | 3 | 0 | 7 | fake |
| novita-dedicated-fake | 2 | 0 | 8 | fake |
| novita-serverless-fake | 9 | 0 | 1 | fake |
| vpc-vllm-fake | 9 | 0 | 1 | fake |
| beam-pod-fake | 9 | 0 | 1 | fake (the adapter itself also ran on a real Beam workspace, see Real surfaces) |
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
| Beam Pod model endpoint (`beam-pod`) | Run for real once on 2026-11-08 (deploy, status, chat, gateway probe, terminate); every other path through the transport fakes |
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

## Spec conformance review (2026-11-08)

The eight success criteria above are narrower than the PRD. They prove one local closed loop (Docker, a fake model, the gateway, the control plane, three packs). This review compared the code with every FR-* requirement, the SPI signatures of TECH-DESIGN sections 5-8, appendices A-E and AC-01..AC-10. Each finding below was confirmed by reading the code path, not a README. "Library only" means the code exists and has unit tests, but no command or service calls it.

### Drift: implemented differently from the spec

| # | Spec | What the code does | Impact |
| --- | --- | --- | --- |
| D1 | AGENTS rule 6, PRD B.8: events are authoritative, rows are projections | `trial/runner.py:_advance` writes `payload.transition` as a dict, but `events/project.py:_transition_for` only honours a string and otherwise guesses from the event type, so `fail_infra`, `pause` and `invalidate` replay wrongly or raise `IllegalTransition`. `verify_projection` has no caller outside tests. The batch driver also upserts trial rows directly | High: a database row cannot be audited against the event log |
| D2 | TD 7.4.2: `runtime: runsc` runs the container under gVisor | `sbx-docker/provider.py` reads `runtime` only to declare L2; `_container_config` never sets the runtime, so the container runs under runc | High: a false isolation claim |
| D3 | TD 7.5, FR-SBX-10: hostd dials out, no inbound port | `hostd/daemon.py` listens (`add_secure_port`) and `hostd/remote.py` connects to the host; only registration and heartbeat go outbound | High for hosts behind NAT (the Mac mini deployment) |
| D4 | PRD A.2 / FR-MOD-09: `trial.versions` carries model revision, template digest, engine, gateway, shell and hostd versions | `core/versions.py:TrialVersions` defines the fields, but nothing in control or runners builds it; only the batch freeze record carries versions | High: US-10 and G7 cannot be answered per trial |
| D5 | FR-MOD-02.5: freeze checks the HF token expiry | The CLI builds `EndpointManager` without `hf_token_expiry`, and a missing expiry blocks the freeze, so every HF model with a `token_ref` cannot be frozen | High for AC-01 |
| D6 | FR-MOD-04: `rpm`/`tpm` are gateway rate limits | `openai-compat` puts them into `UpstreamRoute.default_params`, and `proxy.apply_default_params` merges them into the upstream request body; nothing enforces them | Medium: sent to the vendor as unknown fields |
| D7 | Appendix C.5: the judge panel needs judges from at least two vendors | `cli/context.py` hard-codes `allow_single_judge=True` | Medium |
| D8 | TD 6.3: LiteLLM in front, upstreams in their own protocol | A self-built gateway (recorded decision). Client protocols are converted to chat completions upstream only; a `responses` or `anthropic_messages` upstream returns 502 | Medium: official Anthropic and OpenAI Responses passthrough does not work |
| D9 | TD 4: `harbor_runner` lives in `evalspleef-runners` | It lives in `evalspleef-harbor-bridge/runner.py` and shells out to `harbor run` per turn; the environment bridge builds a fake digest `sha256:<image>` instead of looking up the template registry | Low for layout, medium for the digest |
| D10 | TD 7.6: `templates/registry.json` maps `(env, version)` to OCI, Novita, E2B, Tart and VHDX templates | It holds local Docker fingerprints only | Medium |
| D11 | TD 16.1: the compose stack runs the control plane | The `control` service runs `--help`; the control plane is a command line, not a resident service | Low today, matters for scheduled work (alerts, health loop, ticket expiry) |
| D12 | `tasks/README.md` usage step 3 | Still says to map packs to Harbor tasks (v2 text) | Low |

### Unfinished work

Priorities follow PRD section 5 (P0 is needed for the minimal closed loop). "Account" means the item needs a real vendor account or hardware that this build did not have.

**Evaluation flow and events**

| Item | Spec | State |
| --- | --- | --- |
| Fix the event projection (D1) and call `verify_projection` after every batch run | AGENTS rule 6, B.8 | code fix |
| Batch lifecycle events (Draft, Frozen, Running, Graded) and a persisted batch state; `batch_state` is inferred from trial rows today | B.1, B.8 | missing |
| Emit `hiddentest.result` and `judge.verdict` | B.8 | defined, never emitted |
| Operator commands: regrade, void a trial or version, resolve the manual queue, resume a paused trial | B.6, B.7 | library only (`core/state.py`); paused trials stay paused |
| P5 manual-intervention timeout | B.4 | `manual_interrupt_expired` has no caller |
| Shell heartbeat consumed by the control plane (three missed beats = hung) | B.3 | the runner sends it, nobody reads it |
| Provider-error retry rule (backoff up to five, then back to pending) | B.6 | one three-retry chain for every class |
| Batch total budget checked at dispatch, not only at freeze | B.5 | freeze only |
| Cold-start time excluded from the trial wall clock; dispatch pauses while an endpoint sleeps | FR-MOD-06.2, FR-FLOW-06 | the gateway sends `x-spleef-cold-start-ms`; nothing reads it |
| Judge model and endpoint frozen into the batch | FR-GRD | not in the freeze record |

**Grading, reports, cost, observability**

| Item | Spec | State |
| --- | --- | --- |
| Cell drill-down: model source and HF revision, deployment adapter, engine, sandbox adapter and isolation level, cost basis | FR-RPT-2, NFR-02 | missing; `core/report.py:Cell` has none of these fields |
| Cost columns split by cost basis | FR-RPT-3, PRD Q3 | missing |
| Release package: per-carrier isolation report, judge consistency report, version manifest, probe summary, cost reconciliation | FR-RPT-4, FR-ISO-01 | missing; only the leaderboard exists |
| Judge calibration gate (kappa or alpha >= 0.70) and the regrade agreement gate (>= 95%) as runnable tools | C.5, AC-09, NFR-01 | missing |
| Wire `CostLedger` (token cost, endpoint running time, sandbox seconds) and Novita bill import into batches; cost dashboard | FR-OPS-02, FR-OPS-04, AC-10, NFR-08 | library only, no writer |
| Alerts: orphan sandbox, quota, HF token expiry, frequent cold starts, lost host heartbeat | FR-OPS-03 | idle-endpoint check only, and no resident process runs it |
| OpenTelemetry spans around SPI calls, joined per trial | NFR-10 | `telemetry/tracing.py` has no caller |

**Model side**

| Item | Spec | State |
| --- | --- | --- |
| Populate `trial.versions` (D4) | FR-MOD-09, FR-MOD-07.6 | code fix |
| Record HF token expiry so freeze can pass (D5); schedule the 14-day warning | FR-MOD-02.5 | code fix |
| `verify_mirror` at batch end; honour `mirror: false` from models.yaml (parsed, then dropped) | FR-MOD-02.2, 02.4 | never called |
| Copy chat template and tokenizer hashes into the trial versions | FR-MOD-02.6 | recorded on the spec only |
| Enforce `rpm`/`tpm` in the gateway and stop sending them upstream (D6) | FR-MOD-04 | code fix |
| Gateway: 429/5xx backoff, read timeouts, persistent routes, prices and usage events, a non-zero cost for unpriced routes, per-request round/trial/latency/status record | FR-MOD-07.4, 07.5 | routes, prices and events are in memory; unpriced routes never hit the cost cap |
| Upstream passthrough for Anthropic Messages and OpenAI Responses (D8) | FR-MOD-07.1 | 502 today |
| Vendor-lock mismatch voids the trial as a provider error | FR-MOD-04 | the gateway refuses; the runner is not told |
| Scheduled assisted-ticket expiry (24 h) and a running `HealthLoop` | FR-MOD-03, 06.4 | methods exist, nothing schedules them |
| LoRA adapters on dedicated endpoints | FR-MOD-03.5 | missing (P1) |
| Real Novita Dedicated, Novita Serverless GPU, vpc-vllm and Hugging Face runs; AC-01 with a public, a gated and a private model | AC-01, US-01, US-02 | account |

**Sandbox side**

| Item | Spec | State |
| --- | --- | --- |
| Apply `runsc` in the Docker adapter (D2) | TD 7.4.2 | code fix |
| hostd dials out over one bidirectional stream (D3) | TD 7.5 | redesign of the transport |
| Executable isolation probes instead of prose, and run the shipped `probes/*.sh`; drop the waiver | TD 7.7, appendix D | bridge 2 above |
| Egress enforcement on Docker: internal network, proxy variables, firewall rule; gateway domain added automatically for `in-sandbox` | TD 7.3, FR-SBX-03 | only a network name is attached |
| Docker `create` validates lifetime, disk and resource ceilings against its capabilities | TD 7.1 | disk is silently ignored |
| Multi-carrier template registry and cross-carrier fingerprint check; Tart and VHDX template builds | TD 7.6 | Docker only |
| Harbor: template registry lookup in the bridge, regrade and analyze integration, a run against real Harbor (AC-08) | TD 9, AC-08 | stub executable only |
| A conformance case that proves the TTL sweeper (C-LIFE-03 force-deletes today) and one that exercises the proxy path | TD 15.1 | weak |
| Example `providers.yaml` with a remote host, a Novita instance and egress proxies | TD 14.2 | trimmed to Docker and the mock |
| Real runs: Novita Agent Sandbox (AC-03 second half), E2B self-host, Tart with two macOS VMs for 24 h (AC-04), Hyper-V (AC-05), libvirt, apple-container, Kubernetes, device pool | AC-03..AC-05 | account or hardware |

**Runners, task packs, operations**

| Item | Spec | State |
| --- | --- | --- |
| Native shells (Claude Code, Codex CLI, OpenCode) and Inspect run for real, with pinned versions written to `trial.versions` | FR-RUN-01, 02 | stub executables; versions are `0.1.0` constants |
| Packs for the missing tracks (rust, ios, game) | appendix E.2 | 10 packs cover 9 of 12 tracks |
| Replace the `agent_judge` items in backend-001, macos-001 and video-001 so they finish without a human | appendix C | they always pause |
| Pin image digests (`TODO(pin-on-host)` in every pack) | FR-TSK-03 | the linter accepts a pending marker |
| A resident control-plane service for scheduled work (D11); Helm rendered by `helm`; compose smoke with all services | NFR-09 | not done |
| Throughput and start-latency measurements | NFR-04, NFR-06 | not measured |
| Adapter onboarding drill by an engineer outside the core team | AC-07 | needs a person |
| `docs/guides/` and the stale `tasks/README.md` step (D12) | | not written |

### Acceptance criteria status

| AC | State |
| --- | --- |
| AC-01 | partly: a gated Hugging Face model at a pinned revision was served for real on a Beam RTX 5090 (`beam-pod`) and probed through the gateway (10/14); no Novita GPU had capacity, the public and private cases and a batch on a self-served model have not run |
| AC-02 | partly: gateway budgets proven with a fake upstream; no source or cost-basis annotation on the leaderboard |
| AC-03 | partly: the Docker half runs for real; the Novita adapter passes conformance on the real account and packs now map to Novita templates (D10), but no batch has run on a Novita sandbox |
| AC-04, AC-05 | not met: need a Mac mini with Tart and a Hyper-V host |
| AC-06 | met for Docker and remote-over-Docker (real) and the five in-repo doubles; the other 14 targets pass against transport fakes written together with their adapters |
| AC-07 | not met: needs an outside engineer |
| AC-08 | not met: Harbor not installed |
| AC-09 | not met: no calibration or regrade-agreement tooling |
| AC-10 | partly: the ledger is booked per trial and `spleef batch costs --novita-bill` reconciles it; costs reach it only since aa24625, and no real Novita bill has been reconciled |

## Fixes and real-surface runs (2026-11-08)

### Fixed since the review

| Item | Commit | Regression check |
| --- | --- | --- |
| D1 trial rows are projections of explicit event transitions; `batch run` verifies them | 12b39bc | the old projection fails 30 tests; e2e checks every row |
| D4 `trial.versions` recorded at agent start, copied to the row | 5cb64f3 | e2e asserts sandbox adapter `docker`, deployment `openai-compatible` on all 12 rows |
| D5 HF token expiry recorded in models.yaml (`token_expires`), freeze checks it | a593c97 | mutation: an empty expiry map fails the freeze tests |
| D2 docker runs containers under the configured runtime; an unregistered runtime is refused | b91781e | mutation for both halves on the real daemon |
| Operator commands `spleef trials list/regrade/void/resolve`; grading inputs recorded | dfd1727 | real runner scenarios plus projection check |
| Cost ledger written per trial; `spleef batch costs` reconciles against a Novita bill | 2513a31 | driver and CLI tests |
| `spleef batch report --release DIR`: drill-down, version manifest, probe summary, isolation by carrier, judges, costs | a7c07c3 | e2e asserts every artifact |
| P-09 context probe gave reasoning models no room and measured 0 | df21338 | mutation reproduces the real "recalled up to 0" |
| Gateway retries upstream 429/5xx with backoff (B.6) | 10a9cdd | mutation: no retries fails 3 tests |
| e2b family: creates in flight count against max_concurrency | 696ecf3 | mutation: the race lets 3 of 3 through |
| Novita adapter: commands run in /workspace, `TimeoutException` means lost, templates get /workspace; `novita-live` conformance target | 20b1ba3 | mutation for the cwd default; real conformance below |
| Gateway answers upstream timeouts and dropped connections with 504/502 instead of an unhandled 500; judge timeouts, 429 and 5xx are `EndpointUnavailable` (retried, not invalid) | 67f92a5 | mutation: both halves fail 9 new cases |
| Gateway reports the charged cost (`x-spleef-cost`, constant in the SPI); the judge books the gateway charge without a price table | aa24625 | runner against the real gateway app gets 0.0 on the old code, 0.006 on the new |
| A failure while observing or awaiting feedback ends the observation and fails from verifying (paths docs/02 4.2 already allows), so state and validity agree | e7f8c89 | the old test pinned the stuck state; mutation of core and runner |
| `batch run` checks the freeze guards before warming or retiring any endpoint | 2e8a566 | mutation: the old code printed `warm` then `retire` for a rejected batch |
| D10: the lease manager boots the vendor template each instance maps to (registry `templates` section by OCI digest and instance); the Template Builder can build from a self-contained Dockerfile | d995c4a | mutation of the lease manager, the CLI resolver and the builder |
| Serverless GPU: `rootfs_size_gb`, `min_cuda_version` and a scale `policy` are sent; the live API rejects a create without `policy` (400 "invalid worker policy") although the reference marks it optional | 8861e5d, 156b0fc | mutation; the policy fix was confirmed on the real API |
| Serverless GPU workers can run SGLang (`engine: sglang`), with per-engine parser names in the family profiles | d47023c | mutation |

D3, D6-D9, D11, D12 and the rest of the unfinished list above remain open.

### Real surfaces

| Surface | Result |
| --- | --- |
| Novita LLM API (OpenAI-compatible) | works; 99 models listed |
| DeepSeek API | works; `deepseek-flash`, `deepseek-v4-pro` |
| Novita Agent Sandbox | works. `novita-live` conformance against the real account: 25 passed, 0 failed, 8 not applicable (no create-failure or expiry injection, no network canary, no GUI or ports). Template built by the Template Builder from `python:3.12-slim` by digest |
| Novita GPU instances | API authorised; every create (all 6 products, spot and on-demand) answered `INSUFFICIENT_RESOURCE`; none created |
| Novita Serverless GPU | The adapter's snake_case body matches the v2 reference for `/gpus/v2/endpoints` (an earlier note here compared it with the v1 camelCase API by mistake). Driven through the real adapter on 2026-11-08: a create without `policy` is rejected (400 "invalid worker policy", fixed in 156b0fc); with it, three sync endpoints on the RTX 5090 product `SL-serverless-3` (nvidia/Qwen3.8-27B-NVFP4 at 482ca0f, SGLang dev and v0.5.23 pinned by digest, 80 GB disk, CUDA 12.8) were created and got a worker each, but every worker stayed in `creating` with an empty log for 8 to 16 minutes. A control endpoint with `python:3.12-slim` stayed in `creating` too, and every serverless and instance GPU product reported 0 available, so the pool had no capacity. No worker ever ran, nothing was billed, every endpoint was deleted. Model serving through a Novita GPU is still not verified |
| Beam Pod (`beam-pod`, new adapter 10174a7) | Run for real on 2026-11-08 against workspace `1e1e00`: `BeamPodProvider` deployed orcarouter/Qwen3.8-27B-Uncensored-NVFP4 (gated, revision d350e02) with `lmsysorg/sglang:latest` (v0.5.23, sha256:f832b5c3) on 1x RTX5090, 8 CPU, 48Gi. The launcher (beam-client 0.2.239 in its own uv environment) built the 37 GiB image and deployed in 286 s; the container ran at 342 s and served `/v1/models` at 626 s. A direct chat answered in 5.0 s (166 tokens, reasoning split out by `--reasoning-parser qwen3`); decode ran at about 42 tokens/s. `spleef models register` and `models probe` through the gateway: 10/14 (P-06 `tool_choice: required` ignored, P-11 seed and stop, P-09 and P-10 timed out). The timeouts came from the engine: with `--mem-fraction-static 0.88` the 16.5K-token context probe ran the GPU out of memory (CUDA OOM in the Gated DeltaNet prefill), Beam replaced the container and it served again about 5 minutes later. `teardown('terminate')` stopped and deleted the deployment; the deployment and container lists were empty afterwards. REST shapes (`/gateway/deployments`, `/gateway/containers`, `scale`, `stop`, `DELETE`) matched the fakes |
| Beam Volume weights (`weights_volume`) | Run for real on 2026-11-08 with the Volume `huggingface_models`, filled once by Beam's dashboard import (23.02 GiB at `<volume>/<repo>`, token in the workspace secret `HUGGINGFACE_TOKEN`). The CPU weights Pod ran `snapshot_download` for revision d350e02 and its task (polled through `GET /api/v1/task/{ws}/{id}`) completed in 26 s when probed alone and was already `COMPLETE` at the first poll inside `deploy`; the weights Pod plus GPU deploy took 22 to 25 s (image cached, 286 s on the first run). Weight load measured in SGLang's log: 60.6 s on the first run (HF download in the container), 177.5 s on the first container that read the Volume cold, 5.6 to 5.8 s on later containers. The final run (0.88 memory fraction, `--chunked-prefill-size 2048`) was ready at 161 s instead of 626 s, answered a chat in 6.8 s and was terminated; deployment and container lists were empty afterwards. An attempt with `--mem-fraction-static 0.80` never served: SGLang refused to start because the Gated DeltaNet state cache left room for 0 requests, and Beam restarted the container three times until it was stopped by hand. The 16K-token context probe was not rerun with the chunked prefill |
| Real probes through the gateway | deepseek-flash 11/14, glm-5.3-flash 10/14, qwen3.8-flash 11/14 after the fixes. Remaining failures: P-06 tool_choice modes, P-07 (DeepSeek rejects json_schema), P-11 seed determinism and stop, P-12 a few rate-limited requests |

Real-model batch on local Docker (batch 2026-11-b2, logic-001, data-001 and debug-001, 2 epochs, targets deepseek-flash via the DeepSeek API and glm-5.3-flash via Novita, judge qwen3.8-flash via Novita, all through the gateway): 25 trials including retries, and the event projection matched all 25 rows. deepseek-flash scored 0/Perfect on both epochs of data-001 and debug-001; glm-5.3-flash reached Perfect on data-001 and debug-001 only on the fourth attempt, so with one valid epoch those cells went to Manual; logic-001 stayed Pending for both models (observation timeouts after 300 s, gateway read timeouts). Real defects this run found, all fixed since (see the table above: 67f92a5, aa24625, e7f8c89):

- The gateway did not catch `httpx.ReadTimeout` from a slow upstream; the request failed and the runner crashed, and a judge read timeout was classified as `invalid` instead of a retryable provider error.
- The cost ledger booked 0 USD: the gateway never sent the `x-spleef-cost` header the runner reads, and judge prices were never passed, so no cost reached the trial record, the grade cost or the ledger. Routes still cost 0 unless models.yaml gives `deployment.params.pricing`.
- Four trials that failed while observing stayed in `observing` with validity invalid, because the transition table has no path from observing to invalid.

The first live conformance attempt was invalid: the fake target patched the adapter module process-wide, so `novita-live` ran against the in-memory fake. Building the live target without the fake fixed it; only the third run above counts.

Defects found on real surfaces in that run, fixed since:

- `batch run` retired the endpoints in its `finally` block even when it failed at the freeze guard before any trial (2e8a566).
- A batch could not route a task pack to a Novita sandbox: packs carry OCI templates and the control plane had no OCI-to-Novita template mapping (D10, d995c4a). The mapping is tested against fakes; no batch has run on a Novita sandbox yet, and the Template Builder CLI step that fills the registry is a Python call, not a `spleef` command.
- A streaming response whose upstream times out mid-stream still ends without an error event (the status line is already sent); runners use non-streaming requests, so this only affects streaming clients (open).

Open items from the Beam run:

- The engine flags are a deployment choice, not adapter code: for this model on a 32 GB card, `--mem-fraction-static 0.88` leaves too little memory for a 16K-token prefill, and 0.80 leaves no room for the Gated DeltaNet state cache at all. 0.88 with `--chunked-prefill-size 2048` serves; whether it survives the 16K probe is not verified (open).
- `status` cannot tell an engine restart after a crash from a first start; both read as `deploying` until `/v1/models` answers again, and the crash is only visible in the Beam logs. A crash loop keeps the GPU billed until the deployment is stopped (open).
- A Beam Volume is not faster on a cold read: the first container read the weights in 177.5 s against 60.6 s from Hugging Face; only containers after it read them in about 6 s. The win in that run came mostly from the cached image (open: measure a cold Volume read on another day or node).
- The PostgreSQL store tests give `docker run` 60 s, which includes pulling `postgres:16`; on a machine without the image they fail or error until the image is pulled (open).

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
