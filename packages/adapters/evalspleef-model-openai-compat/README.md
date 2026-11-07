# evalspleef-model-openai-compat

Model endpoint provider for any OpenAI-compatible upstream, in **external** management
mode: the endpoint already exists and the adapter only registers it, verifies it and tells
the gateway how to reach it (TECH-DESIGN section 6.6.1, PRD FR-MOD-04).

Covered upstreams: official vendor APIs, Novita Serverless (`https://api.novita.ai/openai`),
self-hosted vLLM/SGLang and aggregators. Aggregators must configure `provider_lock`; the
gateway then checks the configured response field against the pattern and discards a trial on
a vendor mismatch.

## Configuration

Provider config, overridable per `ModelSpec.deployment.params`:

| Key | Meaning |
| --- | --- |
| `base_url` | Upstream base URL, usually ending in `/v1` |
| `model` | `model` field sent upstream and expected back |
| `api_key_ref` | `SecretRef` resolved through the injected `SecretResolver` |
| `extra_headers` | Additional request headers |
| `protocol` | `chat_completions` (default), `responses`, `anthropic_messages` |
| `rpm` / `tpm` | Rate limits carried on the gateway route's `default_params` |
| `provider_lock` | `{response_field, pattern}` for aggregator vendor locking |
| `default_params` | Extra gateway default parameters |
| `cold_start_window_s` | Retry window the gateway applies to 503/connect timeouts |
| `timeout_s` | Per-request HTTP timeout (default 30 s) |
| `wake_retry_interval_s` | Delay between health probes in `wake` (default 1 s) |

## SPI behaviour

- `validate` checks configuration, resolves the key, and calls `GET /models` when available.
  A 404 on `/models` is tolerated (the model is not checked); a 401 fails the `credential`
  check with a clear reason; a successful list must contain the configured model.
- `plan` is trivial: an external endpoint has no replicas.
- `deploy` returns a `VERIFYING` handle at once.
- `status` sends one `max_tokens=1` health request (protocol-specific path and body):
  success with the expected `model` field is `READY`, 429 is `WARM`, anything else
  (HTTP error, model mismatch, missing field, transport failure) is `FAILED`.
- `wake` repeats the health request: connection failures are retried until the timeout
  and then raised as `EndpointUnavailable`; an HTTP status error maps immediately
  (429 and 5xx to retryable `EndpointUnavailable`, other 4xx to terminal
  `EndpointMismatch`). A successful probe moves the handle to `WARM`.
- `teardown` only marks the handle `RETIRED`; the adapter never shuts an external endpoint
  down.
- `upstream` returns an `UpstreamRoute` with the base URL, upstream model, protocol,
  credential ref, extra headers, rate limits, default params and the provider lock.

All HTTP uses `httpx.AsyncClient` with an explicit timeout; an `httpx` transport can be
injected through the constructor for tests.

## Conformance

`build_targets()` exposes the `openai-compat-fake` target. It runs the provider over real
HTTP against `evalspleef.testing.fake_openai.FakeOpenAIServer`, with injectors
`upstream_status` (429/500/503 for N calls or a time window) and `kill_host`. The target is
marked `fake=True`: the upstream is a test double, so results are **not verified on a real
host** (no vendor API or self-hosted vLLM instance was exercised).

```bash
uv run pytest -p evalspleef.conformance.plugin \
  --spleef-provider openai-compat-fake packages/adapters/evalspleef-model-openai-compat -q
```

The conformance module resolves `evalspleef.conformance` and `evalspleef.testing` at call
time rather than import time because the import-linter contract "adapters depend on spi
only" forbids those static imports for adapter packages.

## Tests

```bash
uv run pytest packages/adapters/evalspleef-model-openai-compat -q
```
