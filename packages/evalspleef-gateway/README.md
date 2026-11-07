# evalspleef-gateway

Model gateway (TECH-DESIGN 6.3, PRD FR-MOD-07): the only door between shells/judges and
model endpoints. It exposes OpenAI Chat Completions, OpenAI Responses and Anthropic
Messages on public route names `ep_<endpoint_id>` while every upstream endpoint is
spoken to as Chat Completions. The pure protocol translators live in `protocols/` and
are not touched by the HTTP layer.

## What it enforces

- **Per-trial virtual keys** (FR-ISO-03, appendix D.4): a key binds one trial to one
  endpoint with a hard budget and an expiry. The plaintext secret is returned once at
  issuance; only its SHA-256 hash is stored. Unknown, revoked or expired keys get 401.
- **Budgets** (appendix B.5): before forwarding, spent cost + a projected minimum
  (one input + one output token) and spent tokens + requested `max_tokens` are checked
  under a per-key lock; over-budget requests get HTTP 429 with `error.code`
  `cost_cap` / `token_cap` (constants in `evalspleef.spi.gateway`). The projection is
  reserved while the request is in flight, so concurrent requests cannot overspend by
  more than the un-projected remainder of requests already in flight. Real usage is
  charged after each response (streams: from the final usage chunk) at the route's
  frozen `PriceSpec`.
- **Endpoint isolation**: a key for endpoint A can never reach endpoint B (HTTP 403);
  `GET /v1/models` lists only the bound route.
- **Cold start absorption**: a route with `cold_start_window_s > 0` retries upstream
  503s and connect timeouts every 15 s (configurable, injectable clock) until the
  window ends; the absorbed time lands in the usage record, the
  `x-spleef-cold-start-ms` response header and an `endpoint.cold_start` event.
- **Provider lock**: when the route declares a `ProviderLock`, the named response
  field must match the pattern; a mismatch returns HTTP 502, emits
  `endpoint.mismatch` and flags the usage record (status 502).
- **No bodies persisted**: usage records carry tokens, latency, status and cost only.

## Layout

| Module | Purpose |
| --- | --- |
| `state.py` | `GatewayState` shared by both apps: routes, prices, key/usage stores, events, per-key accounting locks, clock, upstream client. |
| `proxy.py` | Upstream forwarding as Chat Completions: default params (`max_tokens` ceiling, `stream_options.include_usage=true`), cold-start retry loop, provider-lock check, `OpenedStream` peeking. |
| `data_app.py` | `create_data_app(state)`: `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/models`, `/healthz`; auth, admission, usage recording, `x-spleef-gateway-version` on every response. |
| `admin_app.py` | `create_admin_app(state, admin_token)`: routes/prices/keys/usage/events/version under `/admin`, bearer-token only. |
| `admin_client.py` | `InProcessGatewayAdmin` and `HttpGatewayAdmin`, both implementing the SPI `GatewayAdmin`. |
| `server.py` | `main()` (console script `spleef-gateway`): both apps under uvicorn; config from env/YAML. |
| `store/` | `KeyStore` / `UsageStore` protocols with `MemoryStore`, `SqliteStore` (stdlib sqlite3 in a thread executor) and `PostgresStore` (psycopg 3 async); `open_stores(url)` selects by `memory://`, `sqlite:///<path>`, `postgresql://...`. |

## Running

```bash
export SPLEEF_GATEWAY_ADMIN_TOKEN=change-me
spleef-gateway                     # data on :8390, admin on :8391
```

Environment variables (an optional YAML file via `SPLEEF_GATEWAY_CONFIG` provides the
same keys): `SPLEEF_GATEWAY_ADMIN_TOKEN` (required), `SPLEEF_GATEWAY_DATA_PORT`,
`SPLEEF_GATEWAY_ADMIN_PORT`, `SPLEEF_GATEWAY_STORE`, `SPLEEF_GATEWAY_HOST`.
Upstream API keys resolve from each route's `api_key_ref` at request time through a
`SecretResolver` (`env://NAME` refs read the process environment).

## Admin API

```
PUT    /admin/routes/{name}          body: UpstreamRoute JSON
DELETE /admin/routes/{name}
PUT    /admin/routes/{name}/price    body: PriceSpec JSON
POST   /admin/keys                   body: {trial_id, endpoint_id, max_budget_usd,
                                            max_tokens, expires_at}
                                     returns the TrialKey incl. secret (once)
DELETE /admin/keys/{key_id}          idempotent revoke
GET    /admin/usage?trial_id=
GET    /admin/events?after=<seq>     sequenced JSONL-style event list
GET    /admin/version
```

Events: `key.issued`, `key.revoked`, `request.completed`, `budget.rejected`,
`endpoint.cold_start`, `endpoint.mismatch`.

## Tests

```bash
uv run pytest packages/evalspleef-gateway
uv run python packages/evalspleef-gateway/tests/e2e_gateway_smoke.py
```

`tests/e2e_gateway_smoke.py` boots the `FakeOpenAIServer` plus both gateway apps on
ephemeral ports and drives real HTTP through every behaviour above, writing `curl -i`
style transcripts to `.omo/reports/gateway-core-evidence/`. The Postgres store test
runs against a throw-away `postgres:16` container and is skipped with an explicit
reason when Docker is unavailable.

## Not verified on a real host

Forwarding to real vendor endpoints (OpenAI/Anthropic/Novita) has not been exercised;
all upstream traffic in tests goes to the in-repo fake or scripted transports. The
Postgres store is verified against a real container when Docker is present.
