# EvalSpleef

An agentic LLM evaluation platform. It measures how much help (feedback levels F0-F3) a model needs, within a budget, to bring a real task to the acceptance line. A grade is a deterministic aggregation (tiers P, A, B+, B, C+, C, D, F), so every result can be replayed, regraded and audited.

The platform owns its evaluation kernel and reaches the outside world through two provider interfaces: `ModelEndpointProvider` (where the model under test comes from) and `SandboxProvider` (where task workspaces and tools run). Vendor differences live only in adapters. The command line is `spleef`.

Start with `PLAN.md` for what is built and how each part was verified, `AGENTS.md` for the repository layout, architecture rules and commit rules, and `PRD.md` / `TECH-DESIGN.md` for the requirements and design.

## Set up

```bash
uv sync --all-packages --all-extras      # Python 3.12, whole workspace
git config core.hooksPath .githooks      # once per clone: enables the commit-msg hook
```

## Check

```bash
uv run pytest                            # unit and integration tests (the e2e test is skipped here)
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # strict, every workspace package
uv run lint-imports                      # architecture contracts
python3 scripts/check_repo.py --run      # core tests and the self-test of every task pack
python3 scripts/lint_commit_msg.py --history
```

Parallel agents run tools as `uv run --frozen --no-sync <tool>` and never run `uv lock`, `uv sync` or `uv add`.

## Run the command line

```bash
uv run spleef --help
uv run spleef config validate \
  --models config/examples/models.yaml \
  --providers config/examples/providers.yaml \
  --batch config/examples/batch.yaml
```

`config/examples/` holds commented sample `models.yaml`, `providers.yaml`, `batch.yaml` and `presets.yaml` (model and hardware launch presets, managed with `spleef presets`). `docs/guides/running.md` walks through preparing task packs, models and providers and running a batch. The model gateway is its own service: `spleef-gateway` (environment: `SPLEEF_GATEWAY_ADMIN_TOKEN`, `SPLEEF_GATEWAY_DATA_PORT`, `SPLEEF_GATEWAY_ADMIN_PORT`, `SPLEEF_GATEWAY_STORE`).

## End-to-end batch on local Docker

Needs a running Docker daemon.

```bash
uv run python scripts/build_templates.py --env logic-001 --env data-001 --env debug-001
uv run pytest tests/e2e -m e2e -q
```

The test starts a real `spleef-gateway`, a fake OpenAI-compatible model server and real containers, then drives `spleef models register`, `models probe`, `batch plan`, `freeze`, `run` and `report`. The exact invocations and the resulting report are in `docs/evidence/e2e-batch.md`.

## Adapter conformance

```bash
REPO=$(pwd)
(cd "$(mktemp -d)" && "$REPO/.venv/bin/pytest" --spleef-provider local-docker --spleef-report ./report -q)
```

Run it from an empty directory so only the conformance cases are collected. `--spleef-provider` takes a target name such as `local-docker`, `remote-docker`, `mock-sandbox` or `tart-fake`.

## What is verified, and what is not

Local Docker (directly and through the host agent over mutual-TLS gRPC on loopback), the gateway, the control plane, the PostgreSQL store and the task packs were run for real on one workstation. Everything that needs a vendor account or special hardware (Novita, E2B, Hugging Face, Tart, Hyper-V, libvirt, Apple container, Windows Sandbox, Kubernetes, the device pool) was tested only against fakes of its transport and is **not verified on a real host**. `PLAN.md` has the full table.
