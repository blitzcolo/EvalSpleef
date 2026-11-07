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
| Repository foundation | AGENTS.md, .gitattributes, .gitignore, commit-msg hook, uv workspace, spi, migrated core, schemas, task packs, docs | v2 source | doing |
| Foundations run | testing-doubles, core-v3, task-packs-v3, egress-proxy, gateway-protocols, conformance-kit | Foundation | todo |
| Engines run | gateway-core, control-data, runners, control-config, sbx-docker, sbx-e2b-family, model-openai-compat, model-novita, hostd-remote, control-endpoints, control-sched, control-trial, control-cli | Foundations run | todo |
| Breadth run | sbx-tart, sbx-apple-container, sbx-libvirt, sbx-hyperv, sbx-windows-sandbox, sbx-k8s-agent-sandbox, sbx-device-pool, harbor-bridge, model-vpc-vllm, templates, deploy, docs | Foundations run | todo |
| Integration | End-to-end on real surfaces, conformance on local Docker, review | Engines and Breadth runs | todo |

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

## Verification boundaries

| Surface | Verified how |
| --- | --- |
| Local Docker sandbox, gateway, control plane, runners, hostd over loopback, egress proxy | Run for real on this workstation |
| Postgres store | Real Postgres in a local container when Docker is available |
| Novita Dedicated, Serverless GPU, Agent Sandbox, Hugging Face, E2B self-host, Tart, Hyper-V, libvirt, apple-container, Windows Sandbox, Kubernetes, device pool | Transport fakes only: not verified on a real host or account |

## Progress log

| Item | Status |
| --- | --- |
| Repository files (AGENTS.md, .gitattributes, .gitignore, hook, lint script) | doing |
| Workspace skeleton and lock | doing |
| SPI package | doing |
| Core migrated with its 131 tests | doing |
