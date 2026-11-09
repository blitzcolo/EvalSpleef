# Running EvalSpleef

How to prepare the inputs of a batch and run it with `spleef`. Every command runs from the repository root. The end-to-end test (`tests/e2e`) follows exactly these steps against a fake model; `docs/evidence/e2e-batch.md` shows its transcripts.

## 1. Install

```bash
uv sync --all-packages --all-extras      # Python 3.12 workspace, creates .venv
git config core.hooksPath .githooks      # commit-msg hook
docker info                              # local Docker is the default sandbox carrier
```

## 2. Prepare the data

A batch has four inputs: task packs, environment templates, the three configuration files, and secrets.

### Task packs (`tasks/<task-id>/`)

Ten packs ship in `tasks/` (`tasks/README.md` lists them). A pack holds `task.yaml` (with `runtime.requirements`), `instruction.md`, `persona.md`, `rubric.yaml`, `defects.yaml`, `answers/`, `hidden-tests/run.sh` and `canary.md` (PRD appendix E.1). Hidden tests and answers never reach the agent. To add a pack, follow `docs/05-task-library.md`, then check it:

```bash
uv run spleef lint --tasks-dir tasks                 # pack contract
bash tasks/<task-id>/hidden-tests/run.sh --selftest  # reference A and B pass, the empty agent fails
python3 scripts/check_repo.py                        # schemas and hash locks of every pack
```

`tasks/**` is hash-locked: never reformat a pack's files.

### Environment templates (`templates/`)

Each Linux pack runs in an image built from `templates/<env>/`. Build the ones your batch uses; the build pins each image by digest in `templates/registry.json`:

```bash
uv run python scripts/build_templates.py --env logic-001 --env data-001 --env debug-001
```

### `models.yaml`: the models under test, judges and simulated users

One entry per model, immutable per `(slug, spec_version)`: bump `spec_version` on any change. `config/examples/models.yaml` is a commented example. The fields that matter most:

- `source`: `vendor` (an API model) or `huggingface` (`repo`, a 40-hex `revision`, `access`, `token_ref`, `token_expires`).
- `deployment.adapter` and `deployment.account` (an account in `providers.yaml`): `openai-compatible` for any OpenAI-style API, `beam-pod` for SGLang on Beam GPUs, `vpc-vllm`. `novita-dedicated` / `novita-serverless-gpu` are discontinued (2026-11-09): still in the tree, no longer verified.
- `deployment.params`: adapter parameters. For self-hosted engines, name a preset instead of writing launch arguments by hand (below).
- `pricing_ref`: `api:...`, `ref:...` or `assumed:...`. It sets the cost basis the leaderboard prints for the column; explicit prices go in `deployment.params.pricing` (USD per million tokens).
- `roles`: `target`, `judge` and/or `user`. Judges must come from a different `family` than the models they grade.

An API model, for example:

```yaml
  - slug: deepseek-chat
    spec_version: 1
    family: deepseek
    roles: [target]
    source: {type: vendor, vendor: deepseek, model_id: deepseek-chat}
    deployment:
      adapter: openai-compatible
      account: deepseek          # providers.yaml model_accounts entry
      params: {model: deepseek-chat, pricing: {input_per_mtok: 0.27, output_per_mtok: 1.1}}
    declared_context_tokens: 65536
    declared_max_output_tokens: 8192
    pricing_ref: "api:deepseek@2026-11"
```

### Presets (`presets.yaml`): model and hardware launch arguments

A preset records how one model runs on one hardware SKU: GPU type, count and VRAM, engine and image, launch arguments, the context and concurrency it was measured at, notes (weight size, precision), and a log of verification runs. People write presets; the platform does not search for launch arguments. The table is `presets.yaml` next to `models.yaml` (the top-level key `presets_file` names another file); reference a preset from a model:

```yaml
    deployment:
      adapter: beam-pod
      account: beam
      params: {preset: qwen3.8-27b-uncensored-nvfp4/beam-rtx5090x1/sglang}   # explicit params win
```

At load time the preset is expanded into the parameters; the preset id and its digest are recorded in the registered spec. Manage presets with `spleef presets list | search | show | add | copy | edit | verify | remove` (`config/examples/presets.yaml` has measured examples).

### `providers.yaml`: accounts, sandbox carriers and routes

- `model_accounts`: where each adapter connects, with `api_key_ref` as a SecretRef (`env://DEEPSEEK_API_KEY`, `vault://...`), never the key itself.
- `sandbox_instances`: carriers (`docker`, `remote` for a host running `spleef-hostd`, `novita-sandbox` (discontinued 2026-11-09), `tart`, ...) and their limits.
- `routes`: which carriers a task may use, by OS and role, in order. Name real carriers only.
- `gateway.internal_url`: the gateway data plane the runners call.

### `batch.yaml`: the batch

Models (slugs), the agent shell, task patterns, `epochs` (two or more to rank a cell), `budget.max_total_usd`, the endpoint policy (warm before, sleep or terminate after), the judge panel, and the pricing table version. See `config/examples/batch.yaml`.

### Secrets

Keys stay outside the repository and outside sandboxes. Export them in the shell that runs the gateway and the CLI (or use `vault://` with `SPLEEF_VAULT_URL` / `SPLEEF_VAULT_TOKEN`). The only credential that enters a sandbox is the per-trial gateway key the platform issues.

## 3. Start the gateway

All model traffic goes through the gateway. It needs the upstream keys its routes reference:

```bash
export SPLEEF_GATEWAY_ADMIN_TOKEN=$(openssl rand -hex 16)
export SPLEEF_GATEWAY_DATA_PORT=8701 SPLEEF_GATEWAY_ADMIN_PORT=8702
export SPLEEF_GATEWAY_STORE=sqlite:///$PWD/.state/gateway.sqlite3
export DEEPSEEK_API_KEY=...            # whatever env:// refs your accounts use
uv run spleef gateway serve
```

## 4. Run a batch

In a second shell:

```bash
export SPLEEF_GATEWAY_URL=http://127.0.0.1:8701              # data-plane root without /v1 (providers.yaml gateway.internal_url)
export SPLEEF_GATEWAY_ADMIN_URL=http://127.0.0.1:8702
export SPLEEF_GATEWAY_ADMIN_TOKEN=...                         # the same token
export SPLEEF_STORE_URL=sqlite:///$PWD/.state/store.sqlite3   # or a postgresql:// URL
export SPLEEF_EVENTS_URL=jsonl://$PWD/.state/events
export SPLEEF_STATE_DIR=$PWD/.state
C="--models config/models.yaml --providers config/providers.yaml --tasks-dir tasks"

uv run spleef config validate $C --batch config/batch.yaml
uv run spleef models register $C       # deploys self-hosted endpoints, registers gateway routes
uv run spleef models probe $C          # P-01..P-15 capability probes, frozen into the batch
uv run spleef batch plan  $C --batch config/batch.yaml   # cells, skips with reasons, worst-case spend
uv run spleef batch freeze $C --batch config/batch.yaml  # guards and the version matrix
uv run spleef batch run    $C --batch config/batch.yaml  # every trial, then the leaderboard
uv run spleef batch report $C --batch config/batch.yaml --json --release out/release
uv run spleef batch costs  $C --batch config/batch.yaml
```

`batch run` checks the batch budget before each trial starts, retries infrastructure failures as new trial records, and leaves endpoints as `endpoint_policy.retire_after` says. Retire self-hosted endpoints explicitly with `spleef models retire` when a run is interrupted, so GPUs do not keep billing.

`batch run` prints the report in every case, then exits 1 with one `error:` line when any runnable cell ended `Pending`: such a cell has no result because every trial of it ended `failed_infra` or `invalid` (the line counts the cells and their trials by state; `spleef trials list --batch-id <id>` shows why each failed). Cells in the manual-review queue (`Manual`), scored cells and skipped cells are results and keep the exit code 0. `batch report` and `batch plan` do not apply this check.

## 5. After the run

- `spleef trials list --batch-id <id>`: state, validity and latest grade of every trial.
- `spleef trials resolve`: record a human decision for a trial waiting in the manual queue.
- `spleef trials expire-paused --batch-id <id>`: invalidate trials paused for a human for more than 30 minutes.
- `spleef trials regrade` / `spleef trials void`: regrade from the recorded inputs, or take a trial out of the leaderboard.

Results live in the store (`SPLEEF_STORE_URL`), the append-only event log (`SPLEEF_EVENTS_URL`, the authority every row is projected from), and the release directory written by `batch report --release`: leaderboard, cell drill-down, version manifest, probe summary, judge panel and costs.
