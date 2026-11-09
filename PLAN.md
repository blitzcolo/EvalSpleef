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
| 1 | met | `python3 scripts/lint_commit_msg.py --history` exits 0 (147 commits, 0 failing, before the commit that records it). Running `.githooks/commit-msg` on message files exits 1 for the criterion example `feat: M1 control plane`, for lowercase messages containing `M1`, `P2.1`, `P1` or `W07` (each reported as `matched ...`), for `phase 2`, the word milestone, a `Co-Authored-By` trailer and a session link, and exits 0 for a clean message. `core.hooksPath` is `.githooks` | The hook is client side; `--no-verify` is forbidden by AGENTS.md but not blocked technically |
| 2 | met | `uv run --frozen --no-sync pytest`: 1883 passed, 1 skipped (the e2e test, selected with `-m e2e`), exit 0, 170 s (rerun on 2026-11-08 after the second fix round; rerun on 2026-11-09 after the third round: 1907 passed, 4 skipped in 170 s, and the 4 e2e tests selected with `-m e2e` pass in 47 s; ruff check and format (532 files), mypy strict (301 source files), lint-imports (6 contracts kept), `uv lock --check` and the history lint (151 commits, 0 failing) all exit 0), including the PostgreSQL tests against a real `postgres:16` container. `ruff check .`, `ruff format --check .`, `mypy` (strict, 300 source files) and `lint-imports` (6 contracts kept) all exit 0. `uv lock --check` exits 0. All 131 reference tests are present by name: 129 in `evalspleef-core` (`check_repo.py --run` runs them: Ran 129 tests, OK) and the 2 Harbor mapping tests, which moved with their code to `evalspleef-harbor-bridge/tests/test_events.py` | Run with `--frozen --no-sync`, equivalent to `uv run` because the lock is current |
| 3 | met | `python3 scripts/check_repo.py --run` exits 0: the core unit tests and the self-test of all ten packs on temporary copies (rerun on 2026-11-09 after the third round: exit 0) | web-001 skips Playwright and runs its static contract only |
| 4 | met | `docs/evidence/gateway-curl.md`: a real `spleef-gateway` process driven with `curl -i`: chat with a trial key and a usage record, HTTP 429 `cost_cap` for an over-budget key, 401 for a revoked key, an Anthropic Messages tool round trip | The upstream is the in-repo fake server; no real vendor traffic |
| 5 | met at command level, with a transport fake for the Novita-hosted call | `packages/evalspleef-control/tests/cli/test_models_assisted.py` (5 tests) drives the real `spleef models register`, `tickets`, `attach` and `list`: register leaves the endpoint `awaiting_operator` with an open ticket; attach to `https://fake-endpoint.novita.ai/v1` sends one `max_tokens=1` request, the probes run, the endpoint ends `ready` with a stored probe report and the ticket closes; non-Novita hosts are refused with no request and no state change; the model is never ready without an attach. Mutation check: ignoring the stored report fails the test. Register and probe against a real gateway process are in transcripts 02 and 03 of `docs/evidence/e2e-batch.md` | The adapter accepts only `https` hosts under `*.novita.ai`, so a loopback endpoint cannot be attached by the real adapter; that guard is deliberate and untouched. Not verified against a real Novita account |
| 6 | met | 21 conformance targets, each run as `pytest --spleef-provider <target>`, all exit 0 with 0 failures; see the next table | Only `local-docker` and `remote-docker` are real. Every adapter fake is **not verified on a real host**. Not-applicable counts follow each target's declared capabilities |
| 7 | met | `docs/evidence/e2e-batch.md`: `pytest tests/e2e -m e2e` exits 0 on local Docker with a real `spleef-gateway` process. 3 packs x 2 models x 2 epochs = 12 trials, 6 scored cells. The scripted reference agent scores Perfect (0 deductions) on all three packs, the empty agent scores Failed (28, 20 and 22 deductions), all 12 canary scans are clean, every sandbox ran on `local-docker`, trial events replay through the core state machine, gateway usage matches the runner's token counts | The model and the judge are fakes; see "Known limits" below |
| 8 | met | This table. Items that rest on fakes say so | |

### Conformance runs (criterion 6)

| Target | Pass | Fail | Not applicable | Real or fake |
| --- | --- | --- | --- | --- |
| local-docker (rerun 2026-11-09 after the egress change) | 27 | 0 | 6 | real Docker daemon |
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
| Egress proxy | Run for real on 2026-11-09 in a Docker topology (`tests/egress_docker`: an internal network, a dual-homed proxy built from `deploy/docker/egress.Dockerfile`, two origin servers): the allowlisted host is served, the off-list host gets 403 with an audit line, a direct connection has no route, `off` and `full` are unchanged. Not started in the end-to-end batch; HTTPS to TLS upstreams and package-mirror rewriting inside containers are not verified |
| Docker image templates | logic-001, data-001 and debug-001 built and checked locally; heavy toolchain, macOS and Windows images are not built |
| Compose stack and Helm chart | `docker compose config` validates the compose file; the chart was checked structurally and never rendered by Helm |
| Beam Pod model endpoint (`beam-pod`) | Run for real on 2026-11-08 (deploy, status, chat, gateway probe, Volume weights, long-context runs, terminate); multi-GPU layouts only against the fakes, because the workspace refuses multi-GPU containers |
| Isolation preflight probes | Docker I1-I9 run as commands in real containers (adapter test and every e2e trial); other adapters' probes are described and waived |
| Novita Dedicated and Serverless GPU, Novita Agent Sandbox (all three discontinued 2026-11-09), Hugging Face, E2B self-host, Tart, Hyper-V, libvirt, apple-container, Windows Sandbox, Kubernetes, device pool | Transport fakes only: **not verified on a real host or account** |

## Known limits

The bridges this section used to list (the gateway's extra `ep_` prefix, prose isolation probes waived on Docker, the `/tmp/verify` verifier root, duplicated hash-lock path resolution, the mock sandbox in the example routes, the missing `docs/guides/`, Hyper-V double pid files in the host `/tmp`) are fixed; see "Second fix round" below. What remains:

1. **Observer limit.** When a pack checker aborts early on an invalid answer, other items' violation codes look absent and pass. The trial still fails its hard gates and lands on tier F.
2. **`agent_judge` items.** backend-001, macos-001 and video-001 still contain items no component decides, so those packs pause for a human. The packs are hash-locked content and need an author.
3. **Described probes on the other adapters.** Only the Docker adapter's I1-I9 probes are `$ <command>`s; every other adapter still describes them, and the command line waives those loudly (warning plus `preflight.result` and freeze record entries). Each adapter needs its own commands, checked on its real carrier.

## Discontinued: Novita (2026-11-09)

The owner stopped all Novita work: the platform was hard to use and its GPU pools never had capacity ("Novita 不要管了，他家平台不好用"). Nothing was deleted; removal is a separate decision. What the label covers:

| Piece | State |
| --- | --- |
| `evalspleef-model-novita` (`novita-dedicated`, `novita-serverless-gpu`) | discontinued; stays in the tree with its fake-backed tests and the `novita-dedicated-fake` and `novita-serverless-fake` conformance targets |
| `novita-sandbox` in `evalspleef-sbx-e2b-family` | discontinued; `novita-fake` and the env-gated `novita-live` target stay. The live run of 2026-11-08 (25 passed, 0 failed, 8 not applicable) is the last real-account evidence and will not be repeated. `e2b-selfhost` is not affected |
| Novita as an OpenAI-compatible upstream (LLM API) | not a target of any planned run; `openai-compatible` keeps working for every other vendor |
| D10 (OCI image to Novita template mapping), Template Builder | code stays; no further work |
| PRD items that name Novita: FR-SBX-04, the `novita-dedicated` first-priority deployment, the M0 list | no longer scheduled; the PRD and TECH-DESIGN headers record the change |
| AC-01 | the deployment path moves to `beam-pod` (a gated model was served for real on 2026-11-08); the public and private cases and a batch on a self-served model are still open |
| AC-03 (same Linux task on `docker` and `novita-sandbox`) | cannot be met as written; it needs a second real carrier. Open decision for the owner; apple-container and Tart are candidates on this workstation, neither tried yet |
| AC-10 (reconcile a real Novita bill) | cannot be met; `spleef batch costs --novita-bill` stays as an importer that no real bill will exercise |
| Open items that disappear | Novita GPU capacity, a batch on a Novita sandbox, Novita Serverless GPU serving |

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

Status: D1, D2, D4, D5, D6, D7, D9, D10 and D12 are fixed (commits in the two fix tables below). D3, D8 and D11 are open and listed under architecture-level work.

### Remaining work

Everything from the review's unfinished list that could be fixed in code here is fixed (see "Fixed since the review" and "Second fix round"). What is left needs a design change or an outside resource.

**Architecture-level work** (effort is a rough estimate for one engineer who knows the code)

| Item | Spec | Why it is not a small fix | Effort |
| --- | --- | --- | --- |
| hostd dials out over one bidirectional stream (D3) | TD 7.5, FR-SBX-10 | the transport today is control-plane-to-host gRPC; dial-out needs a new stream protocol, reconnection and request routing on both ends | 1-2 weeks |
| Resident control-plane service (D11): health loop, alerts (orphan sandbox, quota, HF token expiry, frequent cold starts, lost heartbeat), the 24 h assisted-ticket expiry, the periodic `reap(labels={batch})`, the manual-pause expiry on a timer, batch lifecycle events and a persisted batch state | NFR-09, FR-OPS-03, FR-MOD-03, B.1, B.8 | the control plane is a command line; every scheduled duty needs a process that owns time, leader election and restart semantics | 2-3 weeks |
| Resume a paused trial into `running` | B.6 | sandboxes are destroyed when a trial ends; resuming needs sandbox retention for 30 minutes and a live agent session, which only the resident service can hold. A human decision (`spleef trials resolve`) and the timeout (`spleef trials expire-paused`) work today | with the service |
| Pause dispatch while an endpoint sleeps | FR-MOD-06.2 | the cold-start wait is now kept out of the wall clock; pausing the queue needs endpoint state pushed to the scheduler | 2-3 days, with the service |
| D8 remainder: tools and `tool_choice` on an Anthropic Messages upstream, Anthropic-flavoured clients with tools (native shells), an OpenAI Responses upstream | FR-MOD-07.1 | a text-only Anthropic upstream with streaming and structured output exists since 2026-11-09 (see "Claude judge through the gateway"); tool-call translation and Responses passthrough are not built | 3-5 days (estimate) |
| OpenTelemetry beyond the control plane: spans for the gateway, runners and hostd processes, OTLP over gRPC, a collector in the deployed stack | NFR-10 | the control plane's SPI calls are traced since 2026-11-09 (OTLP/HTTP, proven against a local collector); no other process is instrumented by that change | 2-3 days (estimate) |
| Egress enforcement on Docker, what is left: the platform creating the internal network and the proxy per run, a firewall rule, the gateway domain added automatically for in-sandbox shells, HTTPS/TLS and mirror rewriting inside containers | TD 7.3, FR-SBX-03 | the adapter now exports the proxy variables and the topology is proven on real Docker (2026-11-09), but an operator still builds the network and the proxy by hand and nothing adds the gateway domain | 3-5 days (estimate) |
| Multi-carrier template registry and cross-carrier fingerprint check; Tart and VHDX template builds | TD 7.6 | the registry maps OCI digests to Novita templates; Tart and VHDX need their own builders and hosts | 1-2 weeks plus hosts |
| Judge calibration (kappa or alpha >= 0.70) and regrade agreement (>= 95%) as runnable tools | C.5, AC-09, NFR-01 | needs labelled calibration sets and a reviewer workflow | 1 week plus labelled data |
| LoRA adapters on dedicated endpoints | FR-MOD-03.5 | P1 in the PRD; needs adapter support in each engine and the registry | 3-5 days |
| Packs for the missing tracks (rust, ios, game) and replacing the `agent_judge` items of backend-001, macos-001, video-001 | appendix E.2, C | pack authoring, hash-locked content | days per pack |
| Throughput and start-latency measurements | NFR-04, NFR-06 | needs the resident service and a realistic carrier mix | 2-3 days |

**Needs an outside resource**

| Item | What is needed |
| --- | --- |
| Native shells (Claude Code, Codex CLI, OpenCode) and Inspect run for real, with pinned versions in `trial.versions` | the tools installed in task images, and their accounts |
| Harbor integration and AC-08 | a Harbor installation |
| Tart with two macOS VMs for 24 h (AC-04), Hyper-V (AC-05), libvirt, apple-container, Kubernetes, device pool, E2B self-host | the hosts or clusters |
| Not pursued (Novita discontinued 2026-11-09): a batch on a Novita sandbox; Novita Dedicated and Serverless GPU serving | Novita GPU capacity (every product reported 0 available) |
| AC-01 public and private Hugging Face models; a full batch on a self-served model | a private repo and GPU time |
| Multi-GPU (TP2 and above) on Beam | Beam must enable multi-GPU containers for the workspace (`multi_gpu_enabled: false`, max_gpu_count 2, pools not permitted) |
| Adapter onboarding drill (AC-07) | an engineer outside the core team |
| Pin image digests in every pack (`TODO(pin-on-host)`) | the target registries |
| Helm rendered by `helm`; compose smoke with every service | a cluster |

### Acceptance criteria status

| AC | State |
| --- | --- |
| AC-01 | partly: a gated Hugging Face model at a pinned revision was served for real on a Beam RTX 5090 (`beam-pod`) and probed through the gateway (10/14); no Novita GPU had capacity, the public and private cases and a batch on a self-served model have not run |
| AC-02 | partly: gateway budgets proven with a fake upstream; every leaderboard column now carries its cost basis (`cost_bases`, end to end on real Docker) and the drill-down carries model source and revision; not run against a real priced vendor batch since |
| AC-03 | partly: the Docker half runs for real; the Novita adapter passes conformance on the real account and packs now map to Novita templates (D10), but no batch has run on a Novita sandbox, and none will: Novita is discontinued (2026-11-09), so the second real carrier for AC-03 is an open decision |
| AC-04, AC-05 | not met: need a Mac mini with Tart and a Hyper-V host |
| AC-06 | met for Docker and remote-over-Docker (real) and the five in-repo doubles; the other 14 targets pass against transport fakes written together with their adapters |
| AC-07 | not met: needs an outside engineer |
| AC-08 | not met: Harbor not installed |
| AC-09 | not met: no calibration or regrade-agreement tooling |
| AC-10 | partly: the ledger is booked per trial and `spleef batch costs --novita-bill` reconciles it; costs reach it only since aa24625, and no real Novita bill has been reconciled (Novita discontinued 2026-11-09; none will be) |

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

### Second fix round

| Item | Commit | Regression check |
| --- | --- | --- |
| Beam: up to 8-GPU layouts (tp default, explicit tp/pp/dp/ep and dp-attention validated before launch) | c6ca79f | layout tests, mutation |
| Model-and-hardware launch presets: `spleef presets` CRUD, schema validation, expansion at registration with preset id and digest in the spec | a7650a5 | CLI and loader tests |
| Beam: a crash loop ends `FAILED` after two unserved restarts; refused launches carry the SDK reason; multi-GPU permission checked before launch | 5f471e4 | fake transport, mutation |
| Gateway: a stream that fails upstream ends with an error event and a 502/504 usage record | 4b9e31a | chat, Anthropic and Responses streams |
| PostgreSQL tests pull the image before the 60 s start window | 7890c54 | |
| D7: a single judge vendor needs `allow_single_judge_vendor: true` in batch.yaml | 989586a | mutation |
| D6: route `rpm`/`tpm` enforced in the gateway (429 `rate_limited`, `Retry-After`) and never sent upstream | 3972684 | gateway tests, CLI-to-gateway contract test |
| D12: `tasks/README.md` describes running packs through `spleef` batches | 6023efd | |
| Judge panel (models and endpoints) frozen into the version matrix | 557230f | freeze tests |
| `mirror: false` honoured; mirrors rechecked after a batch, a moved mirror opens an incident and fails the run | 3891492 | mutation |
| Engine, GPU, chat template and tokenizer hashes in `trial.versions` | 572c885 | e2e rows |
| D9: Harbor bridge pins task images to the real registry digest | 6e7f7f7 | mutation |
| A trial answered by a vendor the provider lock refuses is voided as a provider error | 8863dc9 | runner test against the fake gateway |
| Docker `create` enforces the requested disk (tmpfs size) and the CPU, memory, disk and lifetime ceilings | efbf615 | real Docker |
| `hiddentest.result` and `judge.verdict` events | 08d6b44 | runner tests |
| A batch refuses models without a price (no unpriced route can dodge the cost cap) | 68955a4 | freeze and run tests |
| Verifier sandboxes get a writable `/verify` tmpfs; the e2e runs on the default root | 1b61437 | real Docker, e2e |
| Hash-lock paths resolved in one place | 10780bb | mutation: 4 tests fail |
| Hyper-V test double keeps guest pid files in the VM directory | 9399442 | host `/tmp` count 0 before and after, 2 with the old code |
| Example routes name real carriers only; commented remote host, Novita sandbox, egress proxy and Beam examples | 2344d6e | `spleef config validate`, e2e |
| Batch budget checked before each trial starts (booked spend plus in-flight worst cases) | ab1ddaa | mutation of both terms |
| Cold-start time kept out of the trial wall clock and logged as `endpoint.cold_start` | ac6318b | mutation |
| Provider failures: five retries with exponential backoff, then back to pending and run after the rest of the batch, once | f4b7b01 | three mutations |
| Three missed heartbeats end the turn as a hung agent (crash retry applies); `AgentRunner.watch_heartbeat` | 9c6cb2f | fake-clock tests, mutations |
| Leaderboard columns carry their cost basis (`cost_bases`, markdown section, note when bases differ) | 52b73bf, fe804e7 | core test, e2e on real Docker |
| Gateway public model name is the endpoint id; the CLI strip and the judge client's re-prefix are gone | fcb5726 | CLI-to-gateway contract test; mutation |
| Preflight probes: `$ <command>` runs in the sandbox; Docker I1-I9 are commands, checked in a real hardened container (and seen failing in an unhardened one); only described probes are waived | 8426fcf | real Docker, waiver test, mutations |
| `spleef trials expire-paused`: trials paused for a human past 30 minutes become invalid | eca7c10 | runner scenario at 29 and 31 minutes |
| C-LIFE-03 proves the TTL reclaim: no forced reap in the case; Docker's injector runs the real sweep | 507972a | mutation: the mock without its sweep fails |

### Third round (2026-11-09)

| Item | Regression check |
| --- | --- |
| The Docker adapter exports `HTTP(S)_PROXY` and `NO_PROXY` to the sandbox in `docs_whitelist` and `package_mirror`; a real-Docker topology (`tests/egress_docker`, 8 tests: internal network, dual-homed proxy, two origin servers) proves it | mutation: `_proxy_env` returning `{}` fails the allowlisted-host test (`assert None == 200`); mutation: proxied modes on the default bridge fails the no-route test; the local-docker conformance run stays 27 passed, 0 failed, 6 not applicable |
| `deploy/docker/egress.Dockerfile` built an image that could not start (`ModuleNotFoundError: No module named 'evalspleef'`: editable workspace links, and a runtime stage without `/workspace`); fixed with `--no-editable` | the old line restored in a temp Dockerfile: build exit 0, `docker run ... --help` exit 1; a test asserts the shipped image, not the bind-mount fallback, ran the proxy |
| Tracing is wired: the registry wraps every sandbox and endpoint provider, `configure_tracing()` installs an OTLP/HTTP SDK provider when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and the CLI flushes at exit | e2e against a local collector: all 12 trials have create, exec, upload and destroy spans with `op`, `provider_instance` and `trial_id`; a run without the variable exports nothing; the unwrapped registry fails the unit test and yields 0 spans in the e2e; a wrapper without `name` fails with the `_BridgedProvider` AttributeError |
| `TracedSandboxProvider` forwards `name` and `close()` | the forwarding unit test lists what the control plane reads |
| New dependency `opentelemetry-exporter-otlp-proto-http` in `evalspleef-control` | `uv lock --check` exit 0 |

Not verified in this round: HTTPS to TLS upstreams through the proxy, package-mirror rewriting inside containers, a non-local collector or OTLP over gRPC, and the other `OTEL_EXPORTER_OTLP_*` variables (headers, timeout). The egress and tracing e2e ran on this Mac's Docker Desktop, not on native Linux dockerd.

### Claude judge through the gateway (2026-11-09)

| Item | Regression check |
| --- | --- |
| A text-only `anthropic_messages` upstream in the gateway: chat requests go to `POST {api_base}/messages` with `x-api-key` and `anthropic-version`; top-level `cache_control` is on by default (`prompt_caching`: false or "1h" in the route defaults); `drop_params` removes parameters a model rejects and the answer lists them in `x-spleef-dropped-params`; `output_config` (effort) passes through; `response_format` json_schema maps to `output_config.format`; streaming converts to chat chunks (thinking as `reasoning_content`, usage chunk with `include_usage`); tools, `tool_choice`, `seed`, images, `n` and `json_object` answer 400 naming the field | mutations, each failing its test: `cache_control` dropped, `Authorization` sent instead of `x-api-key`, cache writes priced at the input price, `response_format` mapping dropped, `thinking_delta` sent as content, `cache_creation_input_tokens` ignored in the stream cost (the `cache_control` and `response_format` ones reproduced independently) |
| Cache-aware cost: `PriceSpec.cache_write_per_mtok`, `compute_cost(cache_write_tokens=...)`; cache reads are recorded as `cached_tokens`, writes only change the cost (the usage store schema is unchanged) | costs computed by hand from the Sonnet 5.5 price table (2.00 input, 10.00 output, 0.10 cache read, 2.50 cache write) |
| `judge_max_tokens` and `judge_timeout_s` in a judge model's `deployment.params` size the judge client (adaptive thinking spends output tokens before the verdict) | `test_judge_limits`; mutation: ignoring the params fails it |
| The run guide told operators to export `SPLEEF_GATEWAY_URL` with `/v1`; the probe client appends `/v1/chat/completions`, so every probe answered 404 | the real batch stopped at `models probe`; the guide now gives the data-plane root without `/v1` |

Live evidence on 2026-11-09, a real gateway process in front of api.anthropic.com, model `claude-sonnet-5-5`, effort high: the same ~3,750-token prompt three times wrote 3,749 cache tokens (cost 0.0094205 USD) and then read 3,749 twice (0.0004229 USD each), matching the hand computation, the gateway usage records and `x-spleef-cost`; two streamed calls wrote 3,747 (0.0100555) and read 3,747 (0.0011927); a `json_schema` request returned `{"answer":"Paris","confidence":10}`. The route is admitted as a judge: P-01, P-02, P-07 and P-14 pass.

Open on this upstream: tools and `tool_choice`, `seed`, images, `n` and `json_object` are rejected by design (the judge route fails P-03 to P-06, P-11 and P-13, none of them required); no mid-stream retries; a request that sets only `max_completion_tokens` reserves 0 tokens in admission (older gap); 1-hour cache writes are priced at `cache_write_per_mtok`; the Responses upstream still answers 502.

### Real-model batch 2026-11-b3 (DeepSeek under test, Claude Sonnet 5.5 judge, 2026-11-09)

Config in `config/examples/real-deepseek/`; local Docker sandboxes, a real gateway process, `deepseek-flash` and `deepseek-v4-pro` on logic-001, data-001 and debug-001, 2 epochs, single judge vendor allowed explicitly. All steps exited 0 after the fixes below (`validate`, `register`, `probe`, `plan`, `freeze`, `run`, `report`); the event projection check covered 14 trial rows.

| Cell | deepseek-flash | deepseek-v4-pro |
| --- | --- | --- |
| data-001 | 0 / Perfect (0.02 USD) | 0 / Perfect (0.14 USD) |
| debug-001 | 0 / Perfect (0.01 USD) | 0 / Perfect (0.17 USD) |
| logic-001 | 3 / Failed (0.03 USD), manual review | 0 / B (0.04 USD), manual review |

logic-001 now finishes for both models (it stayed Pending in batch 2026-11-b2); its two cells wait in the manual queue because their epochs span 3 and 4 tiers. Probes: flash 11/14, v4-pro 10/14 (genuine model limits: tool_choice, strict json_schema, seed); the judge route passes the required P-01, P-02, P-07 and P-14.

Spend per the gateway events: 4.89 USD in total, 3.10 for the judge and 1.77 for the agents. The judge sent 2.25M input tokens, 1.79M of them cache reads (prompt caching), and 178K output tokens, 171K of them adaptive thinking. One judge call failed with a gateway-to-Anthropic ConnectTimeout (504) and the trial was retried automatically; three requests of a v4-pro trial were refused by its key budget (429 `budget.rejected`).

Defects this run exposed, fixed with regression tests: the run guide's `SPLEEF_GATEWAY_URL` ended in `/v1` (every probe 404ed); the Claude judge route could not be admitted until streaming and `response_format` were implemented; the cost ledger booked each trial the judge client's running total, so `spleef batch costs` showed 19.93 USD against 4.89 USD of real spend (`GatewayJudgeClient.complete_metered` and per-session totals now book each run's own charge). Not done: the cost reconciliation against a vendor bill (`mismatch` here only because no bill was supplied), mid-stream retries, and the manual review of the two logic-001 cells.

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
- A streaming response whose upstream times out mid-stream ended without an error event (fixed in 4b9e31a).

Long context and multi-GPU on Beam (2026-11-08, 1x RTX5090, SGLang v0.5.23, 0.88 memory fraction, chunked prefill 2048, context 262,144, weights from the Volume; prompts with unique prefixes so the radix cache cannot share them):

| Case | Result |
| --- | --- |
| Capacity | KV pool 75,515 tokens; at most 2 running requests (the Gated DeltaNet state cache has 12 slots) |
| c=1 | 18K, 37K, 57K and 72K-token prompts served in 2.5 to 13.9 s, the needle found each time; 84K refused with HTTP 400 (longer than the 75,509-token limit), no OOM |
| c=2 | 46K and 69K in total ran concurrently; 93K in total was queued and run one after the other; no OOM |
| c=3 | always 2 running and 1 queued; no OOM |
| 1M at c=1, 2M at c=2, 3M at c=3 | not possible on this model at all: one request is capped at the native 262,144 tokens (default RoPE, no YaRN). Total KV for 1M tokens needs about 32.8 GB (32 KB per token in FP8), more than a 32 GB card holds besides 22.7 GB of weights. Estimated pools: TP2 about 850K tokens, TP4 enough for 1M in total; not measured |
| 2-GPU (TP2) deploy | refused by Beam: multi-GPU containers are not enabled for this workspace (no cost); `validate` now reports it before launch |

The engine flags live in the presets table (`config/examples/presets.yaml`), with these runs as verification records. The P-09/P-10 gateway probes were not re-run after the chunked-prefill change; the long-context run above sent larger prefills through the same engine settings without an OOM.

Open items from the Beam run:

- A Beam Volume is not faster on a cold read: the first container read the weights in 177.5 s against 60.6 s from Hugging Face; only containers after it read them in about 6 s. The win in that run came mostly from the cached image (open: measure a cold Volume read on another day or node).
- Fixed since: crash loops end `FAILED` (5f471e4); the PostgreSQL tests pull their image first (7890c54).

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
