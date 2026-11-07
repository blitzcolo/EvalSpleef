# evalspleef-control

Control plane of the EvalSpleef platform: registry, planner, scheduler, lease
manager, endpoint manager, probes, cost ledger, event store, and the `spleef`
CLI.

## events — append-only event store

The event log is authoritative (PRD appendix B.8); every state change is
appended as an `evalspleef.core.events.EventEnvelope` and database rows are
projections.

- `EventStore` — abstract interface: `append(event) -> seq`,
  `read(batch_id, *, trial_id=None, after_seq=0)`, `subscribe(batch_id, ...)`
  as an async iterator with a bounded wait, and `close()`. Sequence numbers
  are per batch, start at 1, and `after_seq` resumes incrementally.
- `JsonlEventStore` — one append-only file per batch (`<dir>/<batch>.jsonl`),
  optional `fsync=True` durability; sequence numbers are recoverable from the
  file (1-based line numbers).
- `SqlEventStore` — relational `event_log` table through the `store` database
  abstraction; local appends wake subscribers immediately, other processes are
  picked up by a bounded poll.
- `open_event_store(url)` — picks the implementation by URL: `jsonl:///dir`,
  `sqlite:///path`, `postgresql://...`.
- `project_trial_state(events, *, trial_id=None)` — replays
  `evalspleef.core.state.TrialMachine` from a trial's events and returns the
  state; non-lifecycle events are ignored and `payload["transition"]` can
  carry machine events that have no default event type (e.g. `fail_infra`).
- `verify_projection(store, db, *, batch_id)` — checks every `trial` row of
  the batch against the event projection. A row whose state has no backing
  events is reported as a mismatch: state without events is a bug.

## store — relational result store

Portable SQLite/PostgreSQL persistence (TECH-DESIGN section 11 plus the v2
tables). `connect(url)` opens an async `Database` (`sqlite:///<path>` runs
`sqlite3` in a thread executor, `postgresql://...` uses psycopg 3), `migrate()`
applies the SQL files in `src/evalspleef/control/store/migrations/` in order
and records them in `schema_migration`. SQL is the portable subset: `%s`
placeholders, JSON as TEXT, booleans as INTEGER, ISO-8601 timestamps.

`migrations/0001_init.sql` creates: `model_spec`, `hf_artifact`, `endpoint`,
`endpoint_ticket`, `probe_report`, `provider_instance`, `host`,
`template_build`, `sandbox_lease`, `gateway_key`, `cost_ledger` and the v2
tables `trial`, `grade`, `round`, `artifact`, `incident`, `canary`.

Repository classes in `evalspleef.control.store.repos` give typed access per
table via the frozen row dataclasses in `rows.py`. `model_spec` and
`probe_report` rows are immutable (a duplicate insert raises instead of
updating; `ModelSpecExistsError` for model specs); registry tables upsert;
`grade` appends versions, never overwrites. The PostgreSQL tests run against a
throw-away `postgres:16` container started with the docker CLI and removed in
a `finally` block; they skip with an explicit message naming Docker when the
daemon is unreachable.

## ledger — cost ledger and reconciliation

Implements the TECH-DESIGN 6.7 cost model on the `cost_ledger` table.

- `CostEntry` / `CostKind` — entries of kind `tokens`, `gpu`, `sandbox`,
  `infra` with `source`, `amount_usd`, `quantity`, `unit`, `period`, and a
  `reconciled` flag.
- `CostLedger.record` / `entries` / `mark_reconciled` — plain ledger access.
- `import_novita_billing(payload, *, batch_id, period)` — imports the Novita
  billing API JSON (PRD FR-OPS-04; the API filters by `productCategory`).
  Known `product_category` values map onto cost kinds
  (`llm_dedicated_endpoint` → `gpu`, `llm` → `tokens`, `sandbox` → `sandbox`,
  `storage`/`network` → `infra`); unknown categories are skipped, never
  misclassified. Lines are stored with source `novita`.
- `reconcile(batch_id, tolerance=0.05)` — per-kind comparison
  `|Σ ledger − Σ bill| / Σ bill`; a zero bill must match a zero ledger.
  Returns a `ReconcileReport` of `KindReconciliation` rows.
- `allocate_gpu(batch_id, tokens_by_trial)` — per-trial GPU allocation
  `batch.gpu_cost × trial_tokens / batch_tokens` via
  `evalspleef.core.cost.allocate_gpu_cost`.

The exact Novita billing response field names are not pinned by the repository
specs; `import_novita_billing` accepts `items`/`charges`/`transactions`/`data`
lists and both `product_category` and `productCategory` spellings, and is
verified only against that documented shape, not a live Novita response.

## telemetry — tracing helpers

OpenTelemetry integration (TECH-DESIGN section 12). `tracer()` returns a no-op
tracer until the application configures an SDK provider with
`trace.set_tracer_provider`. `traced_provider(provider)` wraps every SPI
method of a `SandboxProvider`, `ModelEndpointProvider`, or live `Sandbox` in a
span with attributes `trial_id`, `provider_instance`, `op`, `bytes`,
`exit_code`, `timed_out` (the ones that apply); sandboxes returned by a traced
provider are traced as well. Tests use the SDK's in-memory span exporter.
