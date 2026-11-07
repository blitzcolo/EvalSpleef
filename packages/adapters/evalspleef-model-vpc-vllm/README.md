# evalspleef-model-vpc-vllm

Self-hosted vLLM and SGLang model endpoint provider for Kubernetes clusters or rented GPU instances (TECH-DESIGN section 6.6.4).

| Entry point | Class | Mode | Description |
| --- | --- | --- | --- |
| `vpc-vllm` | `VpcVllmProvider` | api | Programmatically deploys, scales, wakes, observes, and tears down self-hosted vLLM/SGLang instances |

## Verification status

**Not verified on a real host.** Everything in this adapter was exercised only against test doubles:
- Kubernetes backend is tested against an in-memory `InMemoryKubeApi` test double and manifest snapshot assertions.
- GPU instance backend is tested against an in-memory `httpx.MockTransport` simulating the Novita GPU Instance REST API.
- Inference-plane probing and conformance tests run against a local `FakeOpenAIServer` on an injected clock.

No real Kubernetes cluster or live Novita GPU instances were provisioned during verification.

## Architecture

The adapter operates in `api` management mode: lifecycle operations are fully automated.
Two orchestration backends are implemented behind a unified interface:

1. **Kubernetes (`k8s`)**:
   - Manages one `Deployment` and one `Service` per model endpoint.
   - Manifests are rendered as plain dictionaries and applied through the `KubeApi` protocol.
   - Supports both `InMemoryKubeApi` (for testing/conformance) and `KubernetesClientKubeApi` (which lazily imports `kubernetes`).
   - Upstream URL resolves to the cluster service address: `http://<service_name>:8000/v1`.

2. **GPU Instance (`gpu_instance`)**:
   - Manages rented GPU instances via the Novita GPU Instance REST API (`/gpu-instance/openapi/v1/gpu/instance/*`).
   - Uses `httpx` with support for an injectable `AsyncBaseTransport`.
   - Upstream URL resolves to the instance address: `http://<ip>:8000/v1`.

## Engine Profiles & vLLM Arguments

vLLM startup arguments are derived from per-family YAML profiles located in `engine_profiles/<family>.yaml`:
- Supported families: `qwen`, `llama`, `deepseek`, `glm`, and `generic` fallback.
- Sub-family variants are supported (e.g. `coder` for Qwen, `llama-4`, `v3.1` for DeepSeek, `glm-4.7`).
- Command arguments generated:
  - `--model <mirror_repo or repo>`
  - `--revision <40-hex sha>`
  - `--served-model-name <slug>`
  - `--host 0.0.0.0 --port 8000`
  - `--max-model-len <context_tokens>`
  - `--tensor-parallel-size <gpu_count>`
  - Quantization, tool-call parser, reasoning parser, and extra arguments (e.g. `--trust-remote-code`).
- `deployment.params["engine_overrides"]` allows custom overrides or disabling parsers.
- Hugging Face access tokens (`HUGGING_FACE_HUB_TOKEN`) are injected into the environment from the secret store and are never logged or stored in handle representations.

## Conformance

The adapter registers a conformance target entry point:
- Entry point: `evalspleef.conformance_targets`: `vpc-vllm = "evalspleef_model_vpc_vllm.conformance:build_targets"`
- Target: `vpc-vllm-fake` (`fake=True`), running against `InMemoryKubeApi` and `FakeOpenAIServer`.

Run conformance suite:
```bash
uv run pytest -p evalspleef.conformance.plugin --spleef-provider vpc-vllm-fake --spleef-report /tmp/spleef-vpc-vllm-fake-report.json packages/adapters/evalspleef-model-vpc-vllm
```

Applicability:
- `M-API-01` .. `M-API-03`: Pass against local fake upstream server.
- `M-ID-01`: Pass against local fake upstream server.
- `M-LIFE-01` .. `M-LIFE-04`: Pass against `InMemoryKubeApi`.
- `M-HF-01`: Pass against `FakeHFChecker`.
- `M-ASSIST-01`: Not applicable (provider operates in `api` mode, not `assisted`).
