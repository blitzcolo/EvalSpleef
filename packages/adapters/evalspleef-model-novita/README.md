# evalspleef-model-novita

Model endpoint providers for Novita AI:

| Entry point | Class | Mode | What it does |
| --- | --- | --- | --- |
| `novita-dedicated` | `NovitaDedicatedProvider` | assisted | Renders console instructions, attaches the pasted endpoint, infers state from the inference plane |
| `novita-serverless-gpu` | `NovitaServerlessGpuProvider` | api | Creates, scales, observes and deletes a vLLM endpoint over the Serverless GPUs v2 REST API |

**Not verified against a real Novita account.** Every behaviour here was exercised only against
fakes (an in-memory `httpx.MockTransport` for the REST API, a local OpenAI-compatible server for
the inference plane). Response shapes that the vendor documentation leaves open are marked below.

## Hugging Face access

Both providers carry a class attribute `hf_checker: HFAccessChecker | None = None`. The control
plane assigns it after construction. `validate` uses it for Hugging Face sources and reports a
failed check named `hf_checker_missing` when it is unset.

## novita-dedicated (TECH-DESIGN 6.6.2)

* `validate`: key reference resolves, revision is a 40-hex sha, HF preflight through `hf_checker`,
  weights are `.safetensors` (from `params.weight_files` when given, otherwise from the preflight).
  The key is only *resolved*; it is not exercised against the Novita API until `attach`.
* `plan`: VRAM = (weight bytes + KV cache) x 1.2, KV cache = context x layers x kv heads x
  head_dim x 2 x dtype bytes x target concurrency. The smallest feasible catalog GPU is chosen
  (RTX 4090 24 GB, RTX 5090 32 GB, H100 80 GB, H200 141 GB; more GPUs only if no single one fits).
  Replicas = ceil(peak concurrency / max concurrency per replica). Prices come from PRD 8.1; the
  RTX 5090 has none there, so its cost is unknown unless `config.gpu_prices` supplies one.
  Params: `weight_bytes` or `safetensors_sizes`, `num_hidden_layers`, `num_key_value_heads`,
  `head_dim`, optional `kv_dtype_bytes`, `peak_concurrency`, `max_concurrency_per_replica`
  (16), `planned_hours` (14), `batch_label`, `model` (expected upstream model name).
* `deploy` returns an `AWAITING_OPERATOR` handle; the ticket (`observed["ticket"]`) carries the
  instructions and a 24 h deadline. `status` turns an unattended handle into `FAILED` after it.
* `attach`: `base_url` must be `https` on a `*.novita.ai` host; one `max_tokens=1` chat request
  is sent; the response `model` must equal the attached model. Distinct errors: `InvalidBaseUrl`,
  `ModelNameMismatch`, `CredentialRejected` (401), `AccessForbidden` (403), `DeploymentNotFound`
  (404), `RequestRejected` (400/422), `EndpointUnavailable` (429, 5xx, cold start). A failed
  attach leaves the handle untouched. Then the state is `VERIFYING`.
* `status`: success is `WARM`; an answer slower than 60 s, a read timeout or a 'not ready'
  response is `SLEEPING` (cold start, recorded in `observed`); 401/403/404 are `FAILED`; other
  failures and a drifted `model` field are `DEGRADED`. `status` never mutates `handle.state`.
* `wake` polls (every `wake_poll_interval_s`, default 15) until the endpoint answers or the
  timeout passes (then `SLEEPING`). Only 'not ready' and timeouts are waited out; anything else
  raises its typed error immediately.
* `scale` / `teardown` open operator tickets (`observed["pending_ticket"]`). The control plane
  calls `confirm_ticket(handle)` once the operator reports the console change. A terminate is
  accepted only when the endpoint then answers 404/401; scale-to-zero and `sleep` are accepted
  without a request, because a health request would wake the endpoint.
* `upstream`: `UpstreamRoute` with `cold_start_window_s=480`.

All time goes through an injected clock (`clock=` constructor argument).

Unverified assumptions: the wording of the 'service not ready' answer (PRD 8.1 leaves it open;
markers are `config.not_ready_markers`, default in `inference.py`), and the shape of the
dedicated base URL. The 1.2 safety factor is applied to weights plus KV cache; TECH-DESIGN can
also be read as applying it to the KV term only.

`config.allow_loopback_http` accepts `http://127.0.0.1` base URLs. It exists for the conformance
fake only and defaults to off.

## novita-serverless-gpu (TECH-DESIGN 6.6.3)

* `deploy`: `POST /gpus/v2/endpoints` with `type: sync`, `name`, `product_id`, `image` (must be
  pinned by digest, `config.image`), `command` (`--model <mirror or repo> --revision <sha>` plus
  the generated engine arguments), `envs` (`HUGGING_FACE_HUB_TOKEN` resolved from the secret,
  never logged, recorded masked), `worker_config`, `health_check {path: /health, port}`, `ports`.
  One `GET` right after creation: a serving endpoint is `READY`, otherwise `DEPLOYING`.
* `status`: `GET /gpus/v2/endpoints/{id}`; `status.status`, `workers[]` and `url` map to
  `EndpointState` (`map_status`). Unrecognised values give `DEGRADED` and are recorded in
  `observed["unrecognized_status"]`. A 404 means the endpoint was deleted elsewhere (`FAILED`).
* `scale`: `GET` then `PUT` of the merged `worker_config`. `teardown("sleep")` sets
  `min_replicas=0`; `teardown("terminate")` is `DELETE` (a 404 is accepted).
* `upstream`: `api_base = <url>/v1`; `handle.base_url` is the same value.
* `wake` sends `max_tokens=1` requests until the endpoint answers, then `WARM`.

### API reference pages consulted (2026-11-07)

* Create: <https://docs.novita.ai/api-reference/gpus-v2-create-endpoint> (`POST /gpus/v2/endpoints`)
* Get: <https://docs.novita.ai/api-reference/gpus-v2-get-endpoint> (`GET /gpus/v2/endpoints/{endpoint_id}`)
* Update: <https://docs.novita.ai/api-reference/gpus-v2-update-endpoint> (`PUT /gpus/v2/endpoints/{endpoint_id}`)
* Delete: <https://docs.novita.ai/api-reference/gpus-v2-delete-endpoint> (`DELETE /gpus/v2/endpoints/{endpoint_id}`)
* Index: <https://docs.novita.ai/llms.txt>; vLLM parser names: <https://docs.vllm.ai/en/latest/features/tool_calling/>

Where the reference differs from TECH-DESIGN 6.6.3, the reference won: `request_timeout` lives
inside `worker_config`; `envs` is a list of `{key, value}`; `ports` is a list of integers;
`health_check` is `{path, port}`. The older `serverless-*` pages (camelCase, `workerConfig`,
`state.state == "serving"`) describe a different schema and are not used. The full set of
`status.status` values is not documented (only `running` for endpoints and `ready` for workers
appear in examples), so the mapping in `serverless_gpu.py` is a best reading.

## Engine profiles

`engine_profiles/{qwen,llama,deepseek,glm,generic}.yaml` hold the per-family tool-call parser,
reasoning parser, quantization and extra arguments, with `variants` for sub-families (Llama 4,
DeepSeek V3.1, GLM-4.7, Qwen coder). `engine_args.build_engine_args(spec, ...)` produces the
vLLM argument list (tool-call parser, reasoning parser, `--max-model-len` from the declared
context, quantization, tensor parallel size). `params.engine_overrides` can override or switch off
any of them. Tool-call parser names follow the vLLM tool-calling guide; the reasoning parser
names and the pinned image's vLLM version still need checking against the real image.

## Conformance

`evalspleef_model_novita.conformance:build_targets` returns `novita-dedicated-fake` and
`novita-serverless-fake` (both `fake=True`). The fakes live in that same module, the only module of the adapter that may import
`evalspleef.testing` and `evalspleef.conformance` (the "adapters depend on spi only" import contract
makes exactly this exception). The dedicated fake is a
`FakeOpenAIServer` subclass that can be put to sleep and cold-starts on an injected clock; the
serverless fake is an in-memory implementation of the four REST calls in front of the same
server.

```bash
uv run pytest --spleef-provider novita-dedicated-fake packages/adapters/evalspleef-model-novita
uv run pytest --spleef-provider novita-serverless-fake packages/adapters/evalspleef-model-novita
```

The lifecycle cases (`M-LIFE-*`) are not applicable to the assisted provider, and the assisted
flow case (`M-ASSIST-01`) is not applicable to the api provider.

## Dependencies

`pyyaml` is a runtime dependency (profiles). The `conformance` extra adds `evalspleef-testing`
and `evalspleef-conformance` for the fakes; `uv.lock` has not been refreshed.
