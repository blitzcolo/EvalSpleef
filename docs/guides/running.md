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
- `deployment.params`: adapter parameters. For self-hosted engines, name a preset instead of writing launch arguments by hand (below). Control keys size the clients the platform builds: `probe_timeout_s` (seconds, 60 by default) raises the probe client's ordinary request timeout so a slow thinking model is not failed by a fixed read timeout, and `judge_max_tokens` / `judge_timeout_s` do the same for a judge client (adaptive thinking spends output tokens and time before the verdict). They never reach the upstream API.
- `pricing_ref`: `api:...`, `ref:...` or `assumed:...`. It sets the cost basis the leaderboard prints for the column; explicit prices go in `deployment.params.pricing` (USD per million tokens).
- `roles`: `target`, `judge` and/or `user`. Judges must come from a different `family` than the models they grade, and the panel wants at least two judge vendors (appendix C.5): `config/examples/real-deepseek/` panels Claude Sonnet 5.5 (`anthropic_messages`) with Gemini 4 Carbon on Vertex (`chat_completions`, key in `x-goog-api-key`).

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

- `model_accounts`: where each adapter connects, with `api_key_ref` as a SecretRef (`env://DEEPSEEK_API_KEY`, `vault://...`), never the key itself. Vendors that reject `Authorization: Bearer` take `api_key_header` instead: the gateway then sends the key in that named header and never also as Bearer (for example the Vertex account in `config/examples/real-deepseek/providers.yaml` uses `x-goog-api-key`).
- `sandbox_instances`: carriers (`docker`, `remote` for a host running `spleef-hostd`, `novita-sandbox` (discontinued 2026-11-09), `tart`, ...) and their limits.
- `routes`: which carriers a task may use, by OS and role, in order. Name real carriers only.
- `gateway.internal_url`: the gateway data plane the runners call.

### `batch.yaml`: the batch

Models (slugs), the agent shell, task patterns, `epochs` (two or more to rank a cell), `budget.max_total_usd`, the endpoint policy (warm before, sleep or terminate after), the judge panel, and the pricing table version. See `config/examples/batch.yaml`.

The chat-judge panel (`llm_judge` rubric items) draws its k samples per item for every judge vendor concurrently; `judge_max_parallel_per_vendor` (default 5) bounds how many calls to one vendor may be in flight at once, so a rate-limited vendor is not flooded. Votes and verdicts are recorded in the same order as a serial run. The `agent_judge` samples each boot one judge sandbox and stay sequential.

### Agent judges for `agent_judge` rubric items

Rubric items of kind `agent_judge` (for example backend-001 R11, which needs the service started and three error cases triggered) are decided by judge agents when `batch.yaml` has an `agent_judge` section; without it they stay `inconclusive` with an explicit reason and go to the manual queue. A judge agent is the batch's judge model driven through the gateway with the judge key: each turn it answers one JSON action (`run`, `read_file`, `list_dir`, `write_file`, `submit_verdict`); only the first action of a reply runs, and a `submit_verdict` counts only as the sole action of its reply, so any judge route that serves plain chat completions works, and the route's `prompt_caching` setting (on by default for `anthropic_messages`) caches the growing conversation.

Every sample runs in a fresh judge sandbox from the pack's own image, on a verifier route that declares `writable_paths` and `read_only_upload` (a route without them is refused with `PolicyNotEnforceable`, never degraded): network off except loopback, no credential, the sanitised export read-only at `/artifact`, a writable copy at `/work` (the working directory), and the carrier's own `/tmp` for helper scripts. After each sample the platform hashes `/artifact` inside the sandbox; a change invalidates the sample and counts as an injection attempt. A sample that fails (sandbox, step or time limit, invalid verdict) is `inconclusive`; too many invalid samples send the item to the manual queue. Items that need images (`video_frame`, `screenshot` evidence) stay `inconclusive` with a reason: the protocol is text-only.

```yaml
agent_judge:
  judges: []              # empty: the batch's `judges` serve agent items too
  k: 3                    # samples per item and judge; each boots one sandbox
  max_steps: 12           # executed actions per sample
  max_invalid_replies: 4  # replies without a valid action; the 5th ends the sample
  command_timeout_s: 60
  wall_clock_s: 300
  output_cap_bytes: 8192  # per command, head and tail kept
  run_every_round: false  # judge agents on the final verification only
```

Each item's transcript (every action, the truncated output, the sha256 of the full output, the verdicts) is written as JSON and referenced with its sha256 from the `agent_items` of the `judge.verdict` event. A regrade reads the recorded verdicts and never re-runs a judge agent.

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

`batch costs` reconciles the ledger against a vendor bill: the bill file is parsed by
the account's model provider and imported before the per-kind comparison, e.g.
`uv run spleef batch costs $C --batch-id 2026-11-b1 --bill novita-bill.json --account novita`
(an account whose provider does not implement billing import exits 2 and imports nothing).

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

Prompts come from a seeded generator (`--seed`, random when unset, always recorded). The raw per-request samples go to `$SPLEEF_STATE_DIR/bench/<bench_id>/raw.json`, next to `result.json`; every number in the result is recomputed from the raw file (`evalspleef.core.perf.bench_result`), and the store keeps the result, the raw file's sha256 and the endpoint's hardware and version facts. The cost per million output tokens at saturation is the hourly price (`price_per_hour_usd`, or `price_per_gpu_hour_usd` x `gpu_count` in the deployment params, or `--price-per-hour`) / (system decode tok/s x 0.0036). Hosted APIs are measured the same way and labelled as including the network path and provider-side queueing. Long-context and thinking-mode routes can raise the gateway's 120 s upstream read timeout with the route default `upstream_timeout_s` (in `default_params`; never sent upstream). Admission reserves a request's own output limit (`max_completion_tokens`, else `max_tokens`, the larger when both are sent); a route can reserve `default_reservation_tokens` for bodies that carry neither, and unset (the default) reserves nothing, so admission is not capped by a guessed output size. Like the other control keys it is never sent upstream.

`batch report --release` writes `perf.json` and appends an "Inference performance" section to `leaderboard.md`: per endpoint, the latest benchmark (single-request table, sweep table, saturation and cost) and the in-batch distributions from the gateway's per-request fields (TTFT, TPOT, ITL and E2E percentiles, how many requests had a TTFT, and throughput over the batch's busy time, labelled observed, not saturated).

## 5. After the run

- `spleef trials list --batch-id <id>`: state, validity and latest grade of every trial.
- `spleef trials resolve`: record a human decision for a trial waiting in the manual queue.
- `spleef trials expire-paused --batch-id <id>`: invalidate trials paused for a human for more than 30 minutes.
- `spleef trials regrade` / `spleef trials void`: regrade from the recorded inputs, or take a trial out of the leaderboard.

Results live in the store (`SPLEEF_STORE_URL`), the append-only event log (`SPLEEF_EVENTS_URL`, the authority every row is projected from), and the release directory written by `batch report --release`: leaderboard, cell drill-down, version manifest, probe summary, inference performance, judge panel and costs.

## 6. Judge calibration and agreement

The judge half of appendix C.5 (11.2/11.3), AC-09 and NFR-01 is a set of read-only
commands over the recorded `judge.verdict` events and grades. Every command prints
a human table and accepts `--json`. Exit codes: 0 gate passed, 1 below the gate, 3
not enough data in the store (never confused with a gate failure).

A gate is decided only when every metric it names was computed. Alpha (identical
ratings, `De=0`) and kappa (one rating category, `pe=1`) are mathematically
undefined on degenerate panels; `regrade-agreement` has no comparable tier when
every regrade demands a human. In those cases the metric prints as `-`, the gate
prints `UNDECIDED`, the command exits 3, and `--json` carries
`"gate": {"status": "undecided", "undefined": [{"metric": ..., "reason": ...}]}`.
Percent agreement is still reported. An undecided gate is never a pass: vary the
ratings (or add comparable items) before treating the panel as calibrated.
`batch stability` has no undefined gate: a shared cell set always yields a
computable within-1 share, and no shared cell exits 3.

### Panel health: `spleef judges agreement`

```bash
uv run spleef judges agreement --batch-id 2026-11-b1
uv run spleef judges agreement --batch-id 2026-11-b1 --json
```

Per judge pair Cohen kappa on item verdicts, Krippendorff alpha (nominal) across
all judges with missing data, the pooled percent agreement, the `panel_split`
count (items whose judges disagreed) and `n`. Gate: pooled percent >= 0.70 and
alpha >= 0.667 (C.5 11.3); an undefined alpha leaves the gate undecided (exit 3).

### Blind labelling and calibration (docs/03 11.4, AC-09)

```bash
# 1. export a blind sheet: (trial, item, rubric text, artifact refs) and NO judge verdicts
uv run spleef judges export-labels --batch-id 2026-11-b1 --out labels/sheet.jsonl
# 2. humans fill "label" ("pass"/"fail") per row, independently and blind
# 3. calibrate every judge against the labels (dual gate per judge: percent >= 0.70 and kappa >= 0.70)
uv run spleef judges calibrate --batch-id 2026-11-b1 --labels labels/sheet.jsonl
```

The sheet is JSONL, one row per judged item:
`{"trial_id", "task_id", "item_id", "rubric_text", "artifacts": [{"path", "sha256"}], "label": null}`.
A judge below the gate fails the command with exit 1: fix the rubric, the feedback
text or the judge prompt and recalibrate — never tune the threshold to fit (11.4).
A judge whose kappa is undefined (labels and votes in a single category) leaves
the gate undecided (exit 3), not failed.

### Seed gold set (cold start, weaker than human labels)

```bash
uv run spleef judges seed-gold --tasks-dir tasks --out labels/gold.jsonl
```

Generated from the task packs: reference answers A and B pass every
judge-decidable rubric item, the empty agent fails them. It is a seed set —
expected verdicts, not real trials — and never a substitute for the 30-50 sample
human calibration of docs/03 11.4.

### Regrade agreement (AC-09) and rerun stability (NFR-01)

```bash
# every graded trial regraded from its recorded inputs; exact tier agreement >= 0.95
uv run spleef batch regrade-agreement --batch-id 2026-11-b1
# two or more batches with the same config; share of cells within 1 tier >= 0.90
uv run spleef batch stability --batch-id 2026-11-b1 --batch-id 2026-11-b2
```

`regrade-agreement` lists every disagreement (stored tier vs recomputed tier) and
never writes: it is a check, not a regrade. `stability` compares cell tiers (the
median of the cell's latest recorded grades) across batches and reports the exact
agreement alongside the within-1 share. `regrade-agreement` exits 3 with an
undecided gate when no regrade reproduced a stored tier, because there is then no
agreement share to compare against 0.95.
