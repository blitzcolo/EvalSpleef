# AGENTS.md

Instructions for coding agents and humans working in this repository. Keep it accurate: when a command, path or rule here stops being true, fix this file in the same change.

## What EvalSpleef is

An agentic LLM evaluation platform. It measures how much help (feedback levels F0-F3) a model needs, within a budget, to bring a real task to the acceptance line. A grade is a deterministic aggregation (tiers P, A, B+, B, C+, C, D, F), so every result can be replayed, regraded and audited.

The platform owns its evaluation kernel and talks to the outside world through two provider interfaces (SPIs):

- `ModelEndpointProvider`: where the model under test comes from (any OpenAI-compatible API, Novita Dedicated Endpoint, Novita Serverless GPU, Beam Pods, self-hosted vLLM).
- `SandboxProvider`: where task workspaces and tools run (Docker, Novita Agent Sandbox, E2B self-host, Tart, Hyper-V, libvirt, and others).

Vendor differences live only in adapters. The CLI is `spleef`.

## Source of truth, read before changing contracts

| File | Use it for |
| --- | --- |
| `PRD.md` | Requirements (FR-*, NFR-*, AC-*), shared enums and contracts in appendices A-E |
| `TECH-DESIGN.md` | Architecture, SPI signatures (sections 5-8), config examples (section 14), conformance cases (section 15) |
| `docs/00-conventions.md` ... `docs/08-implementation-notes.md` | Inherited evaluation-flow, grading and isolation specs behind the core package |
| `PLAN.md` | Build plan, work breakdown, progress, and what is verified on real surfaces versus mocks |

If code and these documents disagree, the documents win until they are changed deliberately. Spellings of enums, states, tiers, error classes and budget fields are frozen (PRD appendix A.2); never invent variants.

## Repository layout

```
packages/                  uv workspace members (each has pyproject.toml, src/, tests/)
  evalspleef-spi/            interfaces, data classes, errors, capability types. No logic.
  evalspleef-core/           state machine, grading, feedback, defects, canary, reports. Standard library only.
  evalspleef-control/        registry, planner, scheduler, leases, endpoint manager, probes, cost ledger, event store, CLI
  evalspleef-gateway/        model gateway: trial keys, budgets, protocol conversion, usage events
  evalspleef-runners/        agent runners: unified shell, native shell, inspect runner
  evalspleef-hostd/          host agent and RemoteProvider (mTLS gRPC)
  evalspleef-egress/         egress allowlist proxy and audit logs
  evalspleef-conformance/    pytest plugin: the definition of done for an adapter
  evalspleef-testing/        test doubles: mock sandbox, mock endpoint, fake OpenAI-compatible server
  adapters/evalspleef-*/     one package per model or sandbox provider (vendor SDKs only here)
tasks/<task-id>/           task packs (instruction, rubric, defects, answers, hidden tests, canary)
schemas/                   JSON Schemas for task, rubric, defect, feedback, grade, trial
templates/                 per-environment build definitions (Dockerfile, Tart/Packer, Windows unattend)
deploy/                    docker compose and Helm manifests
config/examples/           models.yaml, providers.yaml, batch.yaml samples
docs/                      inherited specs; docs/appendix holds personas, checklists, playbooks, grading vectors
scripts/                   repository tooling (commit message lint, repo checks)
tests/                     cross-package integration and end-to-end tests
.githooks/                 tracked git hooks (commit-msg)
```

Python import names: `evalspleef.<name>` (shared namespace, no `evalspleef/__init__.py` anywhere) for the nine core packages, and `evalspleef_<kind>_<name>` for adapters, for example `evalspleef_sbx_docker`.

## Toolchain and commands

Python 3.12, managed by uv. Run everything from the repository root.

```bash
uv sync --all-packages --all-extras      # install the whole workspace
uv run pytest                            # all tests
uv run pytest packages/evalspleef-spi    # one package
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # configured package list in pyproject.toml
uv run lint-imports                      # architecture contracts
bash tasks/<task-id>/hidden-tests/run.sh --selftest   # a task pack proves itself
python3 scripts/lint_commit_msg.py --history          # audit commit messages
git config core.hooksPath .githooks      # once per clone: enables the commit-msg hook
```

Parallel agents must run tools with `uv run --frozen --no-sync <tool>` (or `.venv/bin/<tool>`) and must not run `uv lock`, `uv sync` or `uv add`; they race on `uv.lock` and the shared `.venv`. Ask the orchestrator for dependency changes.

## Architecture rules

1. Dependency direction is enforced by import-linter (`uv run lint-imports`).
   - `spi` and `core` import only the standard library.
   - `control`, `gateway`, `runners`, `hostd`, `egress`, `conformance`, `testing` may import `spi`; `control` and `runners` may also import `core`.
   - Adapters import `spi` and their own vendor SDK only (`harbor-bridge` may also import `core`). Adapters never import each other.
   - Vendor names and vendor SDK imports (`novita_sandbox`, `e2b`, `aiodocker`, `libvirt`, `harbor`, `kubernetes` and the like) never appear in `spi`, `core` or `control`.
2. Capability declarations beat assumptions. A provider states what it can do; the scheduler matches `requirements ⊆ capabilities`. If a policy cannot be enforced (for example a network allowlist), reject at creation with `PolicyNotEnforceable`. Never degrade silently, and never use try/except to emulate a missing capability: check capabilities first, and raise `CapabilityNotSupported` for optional operations that are absent.
3. Management plane and data plane are separate. Endpoint lifecycle (deploy, scale, teardown) is the provider's job; all inference traffic goes through the gateway and nowhere else.
4. Every trial gets a fresh sandbox from an immutable template and destroys it afterwards. Snapshots only speed up a template; they never carry state between trials. A trial stays on one sandbox instance and one endpoint for its whole life.
5. Secrets: long-lived credentials (Novita, vendor API keys, HF tokens) exist only in the secret store, the gateway and the control plane. They never enter a sandbox, a log, a config file or a commit. Configs hold `SecretRef` strings such as `vault://hf/eval-readonly`. The only credential allowed in a sandbox is the per-trial gateway key.
6. Events are append-only and authoritative; database rows are projections. A state change without an event is a bug.
7. Grading is a pure function of its inputs (`compute_grade` and friends). No clocks, randomness or global state inside grading, so regrade is idempotent.
8. Record reproducibility fields when data is created (model revision sha, template digest, gateway version, runner version), never reconstruct them later.

## Code conventions

- Python 3.12 with type hints everywhere. `spi`, `control` and `gateway` are type-checked strictly. SPI value types are frozen dataclasses; configuration and wire boundaries use pydantic v2.
- asyncio throughout I/O code. Every network call and subprocess has an explicit timeout. Build subprocess argv as lists; never `shell=True` with interpolated input.
- Raise the typed errors from `evalspleef.spi.errors`; do not swallow exceptions, and do not catch bare `Exception` without re-raising or recording it as an event.
- Comments and docstrings are English and explain intent, not mechanics. Code imported from the earlier reference implementation (`packages/evalspleef-core`) still has Chinese comments; leave them unless you edit the line.
- Adding an adapter: create `packages/adapters/evalspleef-<kind>-<name>/`, register it through entry points (`evalspleef.sandbox_providers` or `evalspleef.model_providers`) in its `pyproject.toml`, ship an isolation profile (sandbox) and pass `evalspleef-conformance`. Write the I1-I9 preflight probes as `$ <command>` (run in the sandbox, exit 0 means the invariant holds) wherever the carrier allows; a described probe is waived, loudly, on every batch. No change to `spi`, `core` or `control` should be needed; if one is, the abstraction is wrong.
- Keep diffs small. No refactors next to a bug fix, no helpers for hypothetical needs, no validation inside trusted code paths (validate at system boundaries only).

## Testing rules

- A behavior change needs a check that can fail for the regression it names. Mock-call assertions, pinned constants and values re-derived from the output under test do not count.
- No fixed sleeps or polling delays. Subscribe to the exact event or state change, then await it with a bounded timeout.
- Mocks must keep the behavior they assert. Core integration tests use `evalspleef-testing` doubles; real-surface runs (local Docker, a local fake OpenAI-compatible server, real Postgres in compose) cover the paths that matter.
- Adapters bound to hardware or credentials we do not have (Tart, Hyper-V, libvirt, apple-container, E2B self-host, Novita, Kubernetes, device pool) are tested with fakes of the transport (fake SDK, fake `subprocess`, recorded SSH sessions). Mark them "not verified on a real host" in `PLAN.md` and the package README; never claim otherwise.
- Never delete, skip or xfail a failing test to get green. Fix the cause or report it.
- Do not write tests that pin prose, prompt wording or documentation text. Test machine-consumed values only.

## Task packs

- Layout and required files: PRD appendix E.1. Hidden tests and reference answers must never be visible to the agent under test (invariant I10).
- `tasks/**` is hash-locked content; `.gitattributes` disables line-ending normalisation there. Do not reformat files in a task pack.
- Every pack declares `runtime.requirements` (PRD FR-TSK-01) and passes `bash hidden-tests/run.sh --selftest` where supported: reference A and B pass, the empty agent fails, hash locks hold.

## Git and commits (hard rules)

Commits are made locally on `main` using the author already configured in git. Do not push, add remotes, change git config, rewrite published history or bypass hooks (`--no-verify`).

Format: `type(scope): imperative summary`, summary lowercase, no trailing period, subject at most 72 characters, blank line before an optional body that explains why.

- Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, `style`, `revert`.
- Scopes: a package short name (`spi`, `core`, `control`, `gateway`, `runners`, `hostd`, `egress`, `conformance`, `testing`, `model-novita`, `sbx-docker`, `harbor-bridge`, ...) or `tasks`, `schemas`, `docs`, `deploy`, `templates`, `repo`.

A commit message must never contain:

- the words milestone or 里程碑, or phase or priority codes such as `M0`, `M1`, `P0`, `P1`, `P2.1`, work-package codes such as `W07`, or numbered labels like "phase 2", "stage 3", "wave 1". The planning documents use these labels internally; describe the change itself instead (`feat(control): add endpoint warm-up`, not `feat: M0 control plane`).
- any `Co-Authored-By`, `Generated-By` or `Assisted-By` trailer, any AI attribution, or tool-generated footers.
- session links, session identifiers or share URLs.
- mentions of the agent tooling used to produce the change.

`.githooks/commit-msg` runs `scripts/lint_commit_msg.py` and rejects violations. Audit existing history with `python3 scripts/lint_commit_msg.py --history`.

Each commit is one logical, verified change that leaves the tree building and its tests green. No WIP commits, no omnibus commits.

## Working as one of several parallel agents

- Edit only the paths your assignment names. Shared files belong to the orchestrator: root `pyproject.toml`, `uv.lock`, `AGENTS.md`, `PLAN.md`, the import-linter configuration, `schemas/`, `packages/evalspleef-spi/` (unless assigned). Report a needed change instead of making it.
- Do not run mutating git commands (`add`, `commit`, `checkout`, `stash`, `reset`). The orchestrator commits.
- Finish with: files changed, commands run with their outcomes, what you could not verify, and any contract question you hit.

## Verification boundaries

`PLAN.md` keeps the authoritative table of what is verified on a real surface (local Docker, local Postgres, local fake model server) and what is only exercised against fakes. Update it whenever that changes.
