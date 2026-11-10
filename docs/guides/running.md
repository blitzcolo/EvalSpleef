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
uv run spleef models bench $C          # inference performance through the gateway (below)
uv run spleef batch plan  $C --batch config/batch.yaml   # cells, skips with reasons, worst-case spend
uv run spleef batch freeze $C --batch config/batch.yaml  # guards and the version matrix
uv run spleef batch run    $C --batch config/batch.yaml  # every trial, then the leaderboard
uv run spleef batch report $C --batch config/batch.yaml --json --release out/release
uv run spleef batch costs  $C --batch config/batch.yaml
```

`batch run` checks the batch budget before each trial starts, retries infrastructure failures as new trial records, and leaves endpoints as `endpoint_policy.retire_after` says. Retire self-hosted endpoints explicitly with `spleef models retire` when a run is interrupted, so GPUs do not keep billing. Before it tears an endpoint down, `retire` asks the provider for the engine log and keeps it as `<state_dir>/endpoint-logs/<endpoint_id>/<utc>.log` with every configured secret value replaced by `***`; the retire event carries `engine_log` with the path, `sha256`, byte count and a `truncated` flag, or `unsupported` / the fetch error. A failed fetch never delays the teardown. The Beam adapter supports it: its log API was checked read-only (filter, paging, row limit), but not a pod's log fetched during its own teardown.

A model with `mirror: true` needs a Hugging Face token that can create repositories; registration rejects it with a clear message otherwise. `mirror` defaults to `false` on every path (`spleef models register` and direct registration alike).

`batch run` prints the report in every case, then exits 1 with one `error:` line when any runnable cell ended `Pending`: such a cell has no result because every trial of it ended `failed_infra` or `invalid` (the line counts the cells and their trials by state; `spleef trials list --batch-id <id>` shows why each failed). Cells in the manual-review queue (`Manual`), scored cells and skipped cells are results and keep the exit code 0. `batch report` and `batch plan` do not apply this check.

### Inference performance: `spleef models bench`

Every inference run records TTFT, ITL, TPOT, end-to-end time and throughput (definitions: PLAN.md, "Design: inference performance metrics in every report"). Two sources feed the report:

- The gateway times every request passively on its monotonic clock (`ttft_ms`, `e2e_ms`, `tpot_ms`, ITL mean, p95, max and count in each usage record and `request.completed` event; `GET /admin/usage` returns them). A request that was not streamed has an end-to-end time only; TTFT, ITL and TPOT stay null, never estimated.
- `spleef models bench` (after `models probe`, which publishes the endpoint's gateway route) measures an endpoint actively through the gateway with streaming requests: single requests at concurrency 1 with unique prompt prefixes, then a closed-loop concurrency sweep that stops when the next level adds less than 5% system decode throughput or requests fail.

```bash
uv run spleef models bench $C --model my-model \
  --shape 1024x256 --shape 8192x256 --sweep-shape 2048x256 \
  --repeats 3 --concurrency 1,2,4,8,16 --requests-per-worker 4 --budget-usd 5
```

Prompts come from a seeded generator (`--seed`, random when unset, always recorded). The raw per-request samples go to `$SPLEEF_STATE_DIR/bench/<bench_id>/raw.json`, next to `result.json`; every number in the result is recomputed from the raw file (`evalspleef.core.perf.bench_result`), and the store keeps the result, the raw file's sha256 and the endpoint's hardware and version facts. The cost per million output tokens at saturation is the hourly price (`price_per_hour_usd`, or `price_per_gpu_hour_usd` x `gpu_count` in the deployment params, or `--price-per-hour`) / (system decode tok/s x 0.0036). Hosted APIs are measured the same way and labelled as including the network path and provider-side queueing. Long-context and thinking-mode routes can raise the gateway's 120 s upstream read timeout with the route default `upstream_timeout_s` (in `default_params`; never sent upstream).

`batch report --release` writes `perf.json` and appends an "Inference performance" section to `leaderboard.md`: per endpoint, the latest benchmark (single-request table, sweep table, saturation and cost) and the in-batch distributions from the gateway's per-request fields (TTFT, TPOT, ITL and E2E percentiles, how many requests had a TTFT, and throughput over the batch's busy time, labelled observed, not saturated).

## 5. After the run

- `spleef trials list --batch-id <id>`: state, validity and latest grade of every trial.
- `spleef trials resolve`: record a human decision for a trial waiting in the manual queue.
- `spleef trials expire-paused --batch-id <id>`: invalidate trials paused for a human for more than 30 minutes.
- `spleef trials regrade` / `spleef trials void`: regrade from the recorded inputs, or take a trial out of the leaderboard.

Results live in the store (`SPLEEF_STORE_URL`), the append-only event log (`SPLEEF_EVENTS_URL`, the authority every row is projected from), and the release directory written by `batch report --release`: leaderboard, cell drill-down, version manifest, probe summary, inference performance, judge panel and costs.
