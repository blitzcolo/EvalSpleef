# Beam: a 1M-token KV pool for Qwen3.8-27B NVFP4 (2026-11-09)

Self-served `orcarouter/Qwen3.8-27B-Uncensored-NVFP4` (revision `d350e02d82203d5bf07a1e203c997581f235f407`) on Beam
through the platform path (`spleef models register` with the `beam-pod` adapter and the preset
`qwen3.8-27b-uncensored-nvfp4/beam-rtx5090x2/sglang-tp2-kv1m`), probed through a real gateway, loaded with about 1M tokens
of concurrent long context, benchmarked, and used as the target of a real batch. Raw data: `.omo/runs/2026-11-b4/`
(gitignored): `longcontext-results.json`, `longcontext-engine.log`, `perf/{single,sweep,metrics}.json`, the batch logs,
the event log and the stores.

## 1. GPUs this workspace can launch, and prices

Workspace `1e1e00`, read 2026-11-09 19:10Z. "Launchable" is `gpus.<type>` from `GET /api/v1/gateway/machines`;
`supportedGpus` matched it for every Blackwell type. `GET /workspace/{id}/limits` lists every SDK `GpuType` member in
`gpu_types` (T4 ... B300, RTXPro6000), so that list is not availability. `GET /workspace/current` now reports
`multi_gpu_enabled: true` (it was false on 2026-11-08), with `max_gpu_count: 2` per container.

| GPU (SDK `GpuType`) | Architecture, NVFP4 | VRAM | Launchable now | Beam price (beam.cloud/pricing, 2026-11-09) |
| --- | --- | --- | --- | --- |
| `RTX5090` | Blackwell, yes | 32 GB | yes | serverless/Pod 1.09 USD/h per GPU, plus GPU-attached vCPU 0.18972 USD/h and RAM 0.0198 USD/GiB-h; on-demand machine "from 0.72 USD/h" |
| `RTXPro6000` | Blackwell, yes | 96 GB | **no** | no serverless price published; on-demand machine "from 1.09 USD/h" |
| `B200` | Blackwell, yes | 180 GB | **no** | no serverless price published; on-demand machine "from 4.11 USD/h" |
| `B300` | Blackwell, yes | - | **no** | no price found on the pricing page |
| `RTX4090` | Ada, FP8 only (no FP4) | 24 GB | yes | 0.69 USD/h (cannot run the NVFP4 layers) |
| `H100`, `H200`, `A100*`, `L40S`, `A6000`, ... | not Blackwell | - | no | (on-demand prices on the page; irrelevant here) |

No usage or billing REST route exists (`/workspace/{ws}/usage` and `/billing` answered 404 on 2026-11-08), so the bill below
is computed from container lifetimes and the published rates, not read from Beam.

## 2. KV pool arithmetic

From `config.json` (text config): 64 layers, `full_attention_interval` 4, so **16 full-attention layers and 48 Gated
DeltaNet (linear) layers**; `num_key_value_heads` 4, `head_dim` 256; KV in FP8 (`kv_cache_scheme` of compressed-tensors).
GDN: 48 value heads and 16 key heads of dim 128, conv kernel 4, `mamba_ssm_dtype` float32.

- KV per token = 16 layers x 2 (K, V) x 4 heads x 256 x 1 byte = **32,768 bytes** (TP2: 16,384 per GPU, 2 KV heads each).
- GDN state per running request = 48 x (48 x 128 x 128 x 4 B + (16 x 128 x 2 + 48 x 128) x 3 x 2 B) = **146.8 MiB** (73.4 MiB per GPU at TP2).
- SGLang v0.5.23 memory accounting (`kv_cache_configurator.py`): rest = free memory after weights - pre_load x (1 - mem_fraction_static) -
  VLM cache reserve (0.1 GiB). With radix cache the GDN pool takes `mamba_full_memory_ratio` 0.9 / 1.9 of the rest and each running
  request needs several slots; with `--disable-radix-cache` it holds exactly `max_running_requests` slots (+1 padding). The rest is the KV pool.
  Post-capture KV sizing is off by default (`SGLANG_ENABLE_POST_CAPTURE_KV_SIZING`), so the activation floor does not apply.

Check against the 1x RTX5090 run of 2026-11-08 (0.88, radix on): the formula gives 84K tokens against 75,515 measured,
so it overestimates by about 0.27 GiB per GPU; that correction is applied below.

| Candidate | Weights per GPU | KV budget per GPU | Estimated pool | Decision |
| --- | --- | --- | --- | --- |
| 1x RTX5090, any flags | 22.7 GB | at most about 6 GiB | about 0.2M | cannot reach 1M |
| 1x RTX PRO 6000 96 GB, 0.90, no radix | 22.7 GB | about 61 GiB | about 2.0M | would be cheapest single GPU; **not launchable** |
| 2x RTX5090 TP2, 0.94, radix on, 4 requests (24 GDN slots) | 11.6 GB | 15.3 GiB | about 1.0M | too tight |
| **2x RTX5090 TP2, 0.94, radix off, 4 requests** | 11.6 GB | 16.5 GiB | **about 1.08M** | chosen (cheapest layout that can pass) |

Per GPU at 0.94: pre-load 30.5 GiB (measured) - weights 11.48 (measured) = 19.0 free; slack 30.5 x 0.06 = 1.83; VLM 0.1;
GDN 5 x 73.4 MiB = 0.36; KV = 16.7 GiB / 16 KiB = about 1.09M tokens.

## 3. Measured pool

| Attempt | Flags (besides TP2, chunked prefill 2048, radix off, 4 running) | SGLang log | Outcome |
| --- | --- | --- | --- |
| spec v1, 19:18Z | `--mem-fraction-static 0.94`, prefill CUDA graphs on | `KV Cache is allocated. dtype: torch.float8_e4m3fn, #tokens: 1093308, K size: 8.34 GB, V size: 8.34 GB`; `Mamba Cache ... max_mamba_cache_size: 4` | died capturing the prefill CUDA graphs (NCCL "unhandled cuda error", 1.41 GB free); torn down after about 4 minutes |
| spec v2/v3 | `0.93`, `--disable-prefill-cuda-graph`, `--disable-custom-all-reduce` | `KV Cache is allocated. dtype: torch.float8_e4m3fn, #tokens: 1073923, K size: 8.19 GB, V size: 8.19 GB`; `max_total_num_tokens=1073923, ... max_running_requests=4, context_len=262144`; weights 11.48 GB per GPU loaded in 5.1 s from the Volume | **served; pool 1,073,923 tokens >= 1M** |

`/get_server_info` confirms `max_total_num_tokens 1073923`, `tp_size 2`, `context_length 262144`, `kv_cache_dtype auto` (FP8 from
`config.json`, as the model card asks). The two RTX 5090s have no peer access (`Setup Custom allreduce failed ... peer access is not
supported`), so NCCL carries the all-reduce over PCIe. Engine: `lmsysorg/sglang:latest@sha256:f832b5c3...` (v0.5.23, CUDA 13 build),
attention backend flashinfer, linear-attention backend triton, decode CUDA graphs for bs 1, 2, 4.

## 4. Long-context proof

Requests sent straight to the Pod URL (Beam token), streaming, unique prefixes (random 64-bit archive id, distinct seeds), a needle
at a random depth, `enable_thinking: false`.

**c=1:** 248,331 prompt tokens: TTFT 138.8 s (1,789 tokens/s prefill), needle found, decode 69.7 tokens/s, no OOM.
A first try at 269,260 tokens was refused with HTTP 400 (longer than 262,144), as expected.

**Four at once (about 993K in total), all submitted together:**

| Seed | Prompt tokens | TTFT s | Ended s | Needle | Result |
| --- | --- | --- | --- | --- | --- |
| 200 | - | - | 301.7 | no | RemoteProtocolError: Server disconnected without sending a response. |
| 201 | 248154 | 255.6 | 307.8 | yes | ok |
| 202 | 248175 | 133.0 | 307.9 | yes | ok |
| 203 | - | - | 301.7 | no | RemoteProtocolError: Server disconnected without sending a response. |

The engine prefills one request at a time (about 1.4-3.3K tokens/s per chunk as context grows); the two requests still queued for
prefill were cut at exactly 301.7 s by Beam's ingress, which drops a request that sends no bytes for 300 s. The engine saw no error
(no OOM, no retraction) and aborted the two disconnected requests.

**Four overlapping (each starts when the previous streams its first token; 4,096 output tokens with `ignore_eos` so all stay resident):**

| Seed | Prompt tokens | Output | Sent at s | First token at s | TTFT s | Prefill tok/s | Ended at s | Needle |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 400 | 248,242 | 4,096 | 0.0 | 127.0 | 127.0 | 1,955 | 600.7 | yes |
| 401 | 248,287 | 4,096 | 127.0 | 256.5 | 129.4 | 1,918 | 609.6 | yes |
| 402 | 248,251 | 4,096 | 256.5 | 391.6 | 135.1 | 1,838 | 621.8 | yes |
| 403 | 248,305 | 4,096 | 391.6 | 519.9 | 128.3 | 1,936 | 625.9 | yes |

From 519.9 s to 600.7 s all four requests ran together. Engine log at the peak: `Decode batch, #running-req: 4, #full token: 1006884,
full token usage: 0.94, mamba num: 4, mamba usage: 1.00, cuda graph: True, gen throughput (token/s): 146.68` (73 such lines, no OOM,
no retraction, no abort). So **1,006,884 tokens of KV were resident at once**, four requests of about 250K each, every needle retrieved.
Decode of a running request while another one prefills drops to about 4-12 tokens/s (prefill chunks take the GPU); alone it runs at
about 52-70 tokens/s.

## 5. Probes through the gateway (P-01..P-15)

| Spec | Result | Failures |
| --- | --- | --- |
| v2 (thinking on) | 11/14, required P-01, P-02, P-14 pass | P-06 `tool_choice: required` ignored; P-10 504 (a 32K-token output at about 64 tokens/s outruns the gateway's 120 s upstream timeout); P-11 seed and stop |
| v3 (non-thinking, used for the batch) | **13/14**, required pass | P-11 seed determinism (stop honoured) |

P-09 (context) passed in both, which timed out on the one-GPU run. Judge route (Claude Sonnet 5.5): 7/14, its required P-01, P-02,
P-07 and P-14 pass (tools are not supported on `anthropic_messages` upstreams).

## 6. Inference performance (through the gateway on 8721)

Client-side scripts in `.omo/runs/2026-11-b4/perf/` (`perf.py`, `analyze.py`): streaming requests through the gateway, one monotonic
clock (`time.perf_counter`), raw per-request timestamps in `single.json` and `sweep.json`, metrics recomputed by `analyze.py` into
`metrics.json`. Definitions as in PLAN.md "Design: inference performance metrics in every report". Target spec v3 (non-thinking),
`ignore_eos`, 256 output tokens, `temperature 0`, unique prefixes (radix cache is off anyway; `cached_tokens` was never reported, so
counted as 0). Hardware and engine: 2x RTX 5090 (Beam Pod, 8 vCPU, 48 GiB), SGLang v0.5.23 TP2, FP8 KV, chunked prefill 2048,
`--mem-fraction-static 0.93`, `--max-running-requests 4`, radix cache off, prefill CUDA graphs off, decode CUDA graphs on.
The client is on a Mac on a home connection; TTFT and ITL include the gateway and Beam's ingress round trip.

**Single request (concurrency 1, 3 repeats each):**

| Input (tokens) | Output | OK | TTFT p50 s | Prefill tok/s p50 | Decode tok/s p50 | TPOT p50 ms | ITL mean / p50 / p95 / p99 ms | E2E p50 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ~1K (1094-1105) | 256 | 3/3 | 1.23 | 895 | 64.9 | 15.4 | 15.3 / 11.5 / 46.0 / 97.8 | 5.13 |
| ~8K (8819-8856) | 256 | 3/3 | 3.55 | 2491 | 64.5 | 15.5 | 15.5 / 12.8 / 42.1 / 84.9 | 7.53 |
| ~64K (74108-74217) | 256 | 3/3 | 28.23 | 2628 | 61.0 | 16.4 | 16.5 / 12.3 / 51.0 / 123.2 | 32.66 |
| ~248K (248,331) | 7 | 1/1 | 138.8 | 1,789 | about 70 | - | - | 138.9 |

The ~248K row is the c=1 long-context request above: sent straight to the Pod, because through the gateway its 139 s TTFT would pass
the gateway's 120 s upstream read timeout. Only TTFT, prefill speed and a rough decode rate were recorded for it (7 output tokens).

**Concurrency sweep (about 2.1K input, 256 output, closed loop for 60 s per level, steady window = run minus first and last 10%):**

| Concurrency | OK | Window (requests) | System prefill tok/s | System decode tok/s | TTFT p50 / p95 / p99 s | TPOT p50 / p95 / p99 ms | ITL p50 / p95 / p99 ms | E2E p50 / p95 / p99 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 11/11 | 50.7 s (9) | 379.2 | 45.5 | 1.31 / 2.87 / 2.97 | 15.6 / 16.0 / 16.1 | 11.6 / 44.7 / 120.2 | 5.33 / 6.84 / 6.94 |
| 2 | 16/16 | 49.0 s (14) | 608.3 | 73.2 | 2.33 / 10.45 / 11.02 | 16.3 / 19.4 / 19.6 | 14.0 / 46.4 / 93.6 | 6.53 / 15.14 / 15.22 |
| 4 | 32/32 | 49.5 s (28) | 1203.8 | 144.8 | 2.92 / 3.50 / 3.60 | 18.6 / 23.8 / 24.5 | 13.9 / 49.7 / 93.3 | 7.72 / 8.08 / 8.11 |
| 8 | 40/40 | 56.1 s (32) | 1213.6 | 146.0 | 9.28 / 9.95 / 10.08 | 18.2 / 21.6 / 21.7 | 10.9 / 53.5 / 127.4 | 13.94 / 14.32 / 14.33 |

Saturation is reached at **concurrency 4** (the engine's `max_running_requests`): c=8 adds 0.8% system decode (below the 5% rule) and
only queues (TTFT p50 rises from 2.9 s to 9.3 s). Full-load figures are the c=4 row: system decode **144.8 tokens/s**, system prefill
1,204 tokens/s (at 2.1K-token prompts and 256-token outputs), TTFT p50 2.9 s, TPOT p50 18.6 ms, ITL p50 13.9 ms, E2E p50 7.7 s.
Long-context load: 146.7 tokens/s aggregate decode with four ~250K contexts resident (engine log above).

**Derived cost at saturation:** 4.65 USD/h / (144.8 tokens/s x 0.0036) = **8.92 USD per million output tokens**. The written formula
"hourly price / (tok/s x 3.6)" gives USD per thousand tokens (tok/s x 3.6 = thousands of tokens per hour); per million it is x 0.0036.
The assumed price in `config/examples/real-beam/models.yaml` (8.61 USD per million output, 0.30 per million input, from an assumed
150 tokens/s decode and 4,300 tokens/s prefill) is close for output; measured prefill (about 1.2-2.6K tokens/s) makes input
about 2-3x more expensive than assumed.

## 7. Real batch

Config `config/examples/real-beam/` (target `qwen38-27b-nvfp4-beam`, judge `claude-sonnet-5-5` through the gateway, local Docker
sandboxes, logic-001, data-001 and debug-001, 2 epochs, `max_total_usd: 50`; plan printed a worst case of 44.00 USD, so the freeze
guard passed without raising the cap). State in `.omo/runs/2026-11-b4/` (the directory name is the first batch id).

**2026-11-b4 (spec v2, thinking on): stopped.** Every model call returned 504 from the gateway: in thinking mode an agent turn ran
past the gateway's fixed 120 s upstream read timeout (non-streaming calls, about 56 tokens/s per request with four running). All four
first trials ended `invalid` (`RunnerCrashed` after a retry). I stopped the run after about 15 minutes instead of letting it retry
into the same wall (agent spend 0.03 USD) and removed its four sandbox containers.

**2026-11-b5 (spec v3, non-thinking via `default_params`):** `plan`, `freeze` and `run` exited 0, `report` exited 0; 7 trials
(one judge-side retry: Anthropic dropped a connection, gateway 502, the trial was invalidated and rerun as designed); the event
projection matched all 7 rows; `retire_after: terminate` tore the endpoint down.

| Cell | qwen38-27b-nvfp4-beam (2x RTX 5090) | Epoch tiers | Agent cost per epoch (assumed price) | deepseek-flash (b3) | deepseek-v4-pro (b3) |
| --- | --- | --- | --- | --- | --- |
| data-001 | **0 / Perfect** | P, P | 0.25, 0.05 USD | 0 / Perfect (0.02) | 0 / Perfect (0.14) |
| debug-001 | **0 / Perfect** | P, P | 0.05, 0.12 USD | 0 / Perfect (0.01) | 0 / Perfect (0.17) |
| logic-001 | **0 / A** | A, P | 0.06, 0.05 USD | 3 / Failed (0.03), manual review | 0 / B (0.04), manual review |

On these three packs the self-served 27B NVFP4 model in non-thinking mode matches both DeepSeek models on data-001 and debug-001 and
does better on logic-001 (A and P, no manual review, where flash failed and v4-pro scored B with tiers spread over 3-4 grades). Three
packs and two epochs are a small sample; this is not a ranking. Agent cost is the assumed per-token price (section 6), not a vendor
charge; the GPU bill is section 8.

## 8. Spend

Container lifetimes from the REST records and the run logs, priced at 4.65 USD/h (2x 1.09 GPU + 8 x 0.18972 vCPU + 48 x 0.0198 GiB RAM):

| Deployment | From | To | Minutes | USD |
| --- | --- | --- | --- | --- |
| v1 (0.94, crashed in graph capture) | 19:18:49Z | about 19:23Z (retired) | about 4 | 0.31 |
| v2 (pool proof, probes, long context, batch b4) | 19:23:55Z | 20:20:47Z | 56.9 | 4.41 |
| v3 (probes, perf benchmark, batch b5) | 20:21:20Z | 21:08:34Z | 47.2 | 3.66 |
| **GPU total** | | | **about 108** | **about 8.4** |

The weights Pods (2 vCPU, 8 GiB, under a minute each, weights already in the Volume) add well under 0.05 USD. Gateway-booked token
spend: batch b5 1.25 USD (agent 0.76 at the assumed price, which is not an extra charge since the GPU hours already pay for it; judge
about 0.50 USD of real Anthropic spend), batch b4 0.03 USD agent. **Real money spent: about 8.4 USD Beam (computed, not billed yet)
plus about 0.5 USD Anthropic**, well under the 50 USD guard; the 6-hour watchdog never fired. `spleef batch costs` books 0 USD of
`gpu`, because nothing feeds GPU hours into the ledger for a self-served endpoint, and it reports `MISMATCH` because no vendor bill
was supplied (exit 1, as in batch b3).

## 9. Teardown receipt

At 21:10Z `GET /api/v1/gateway/deployments` and `/gateway/containers` returned empty lists for the whole workspace; the gateway
(8721/8722) was stopped and both ports were free; no `spleef gateway serve` or watchdog process remained; no sandbox container of
batch b4 or b5 remained in Docker; the run's temporary directory was removed (traces copied to `.omo/runs/2026-11-b4/traces/`).

## 10. Defects found and what changed

- `beam-pod` `validate` failed every Hugging Face model with `hf_checker_missing`: it demanded an `HFAccessChecker` on the provider that
  nothing in the control plane assigns (the endpoint manager runs its own HF preflight before `validate`). Fixed in the adapter: an
  unset checker is not a failure; an injected one still adds its checks. Regression test
  `test_validate_passes_without_an_injected_hf_checker` (fails on the old code with `['hf_checker_missing']`).
- `beam-pod` routes dropped `default_params`, so a model's request defaults (here the thinking mode) could not reach the gateway route.
  Fixed: `upstream()` passes `default_params` from the spec (account config as fallback). Regression test
  `test_the_route_carries_the_specs_default_params` (fails on the old code: `{}` vs the defaults).
- New preset `qwen3.8-27b-uncensored-nvfp4/beam-rtx5090x2/sglang-tp2-kv1m` with this run in its verification log.

Contract questions (outside this lane's scope, not changed):

1. No CLI command advances an API-mode endpoint from `deploying` to `verifying`: `EndpointManager.refresh()` exists but has no caller,
   and `models probe` refuses `deploying`. This run called `refresh()` through `AppContext` from a one-off script. `models register`
   (with a wait) or `models probe` should poll `refresh()`.
2. `models retire` exits 1 after a successful Beam teardown when the endpoint never got a gateway route (`DELETE /admin/routes/... 404`).
3. The gateway's upstream read timeout is fixed at 120 s (`GatewayState(upstream_timeout_s=120.0)`, no setting in `server.py`). A
   self-served model in thinking mode at about 56 tokens/s per request cannot answer a non-streaming agent turn in time; the batch had to
   switch the target to non-thinking mode. A per-route timeout, or streaming upstream while answering the client non-streamed, would fix it.
4. `HuggingFaceChecker.ensure_mirror` defaults to creating a private mirror under `evalspleef-mirrors`; with a read token that cannot work,
   so self-served specs need `mirror: false`.
5. The perf-metrics formula in PLAN.md (x 3.6) gives USD per thousand tokens, not per million.

## 11. What is NOT proven

- A 1M-token KV pool on one GPU: no Blackwell card with more than 32 GB was launchable (RTX PRO 6000, B200 and B300 unavailable).
- Four ~250K requests submitted at the same instant through Beam's ingress: two were cut at 300 s without bytes; the overlap proof
  staggered them. The engine-side 1M residency is proven; the end-to-end simultaneous case is not, and not through the gateway at all.
- Long context through the gateway: anything with a TTFT above 120 s fails there (contract question 3).
- Thinking-mode quality: the batch ran the target in non-thinking mode.
- The bill: no Beam usage API exists, so spend is computed from container lifetimes and list prices, not reconciled with an invoice.
- Cold Volume reads on a new node, and Pod pricing being identical to serverless pricing (assumed from the pricing page).
- TP2 over PCIe performance against NVLink hardware, and any behaviour of the engine past 4 running requests (the GDN slots cap it).
