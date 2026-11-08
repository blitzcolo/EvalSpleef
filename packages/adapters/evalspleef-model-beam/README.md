# evalspleef-model-beam

Model provider `beam-pod` (management mode `api`): an OpenAI-compatible SGLang server for a
pinned Hugging Face revision, deployed as a [Beam](https://www.beam.cloud) Pod with a stable,
token-protected URL. All inference traffic still goes through the gateway; the gateway calls the
Pod URL with the Beam token as its upstream key.

## How it talks to Beam

| Operation | Surface |
| --- | --- |
| `deploy` | `launcher.py` in a child process: the Beam SDK (`beam-client`) builds the registry image and runs `Pod(...).deploy(name)`, then `POST /deployments/{id}/scale` starts the planned container count |
| `status` | `GET /deployments` and `GET /containers` (gateway REST); a `RUNNING` container is `ready` once `GET <url>/v1/models` answers 200 |
| `scale` | `POST /deployments/{id}/scale {containers}`; a Pod deployment holds a fixed count, so `max_replicas` is provisioned |
| `teardown sleep` / `terminate` | scale to 0 / `POST /deployments/{id}/stop` then `DELETE /deployments/{id}` |
| `wake` | scale back to one container when asleep, then `max_tokens=1` chat probes until it answers |

The gateway REST base is `https://app.beam.cloud/api/v1/gateway` (`gateway.swagger.json` under
<https://docs.beam.cloud/v2/reference/api-docs/>). The gateway can answer HTTP 200 with
`ok: false`; both layers are checked.

### Why a child process

`beam-client` (with `beta9`) requires protobuf < 7 and `evalspleef-hostd` requires protobuf >= 7,
so the SDK cannot live in the workspace environment. The launcher runs with
`uv run --no-project --python 3.12 --with beam-client==0.2.239 python -I launcher.py`
(override with `sdk_requirement` or `launcher_argv`). It starts in an empty scratch directory with
`CONFIG_PATH` inside it, so the SDK syncs no local files (registry images ignore `*` anyway) and
never touches `~/.beam`. The Beam token reaches the child only through `BEAM_TOKEN` in its
environment; the request, including the HF token, arrives as JSON on stdin, never in argv. A
launch that fails or times out removes any deployment of the same name it may have left.

## Configuration

Account config (`providers.yaml` `model_accounts.<name>`):

| Key | Default | Meaning |
| --- | --- | --- |
| `api_key_ref` | `env://BEAM_TOKEN_<ACCOUNT>` | SecretRef of the Beam API token (management and the Pod URL) |
| `image` | required | digest-pinned engine image, e.g. `lmsysorg/sglang:latest@sha256:...` |
| `sdk_requirement` | `beam-client==0.2.239` | pinned SDK the launcher runs with |
| `launch_timeout_s` | 900 | deadline of the launcher (image build included) |
| `cold_start_window_s` | 0 | passed to the gateway route |

Deployment params (`models.yaml` `deployment.params`): `gpu` (required, e.g. `RTX5090`),
`gpu_count`, `cpu` (8), `memory` (`32Gi`), `min_replicas`/`max_replicas` (1), `max_concurrency`
(8, becomes `--max-running-requests`), `context_length` (declared context), `keep_warm_s` (1800;
`-1` keeps containers until stopped), `engine_extra_args` (appended to the SGLang command),
`beam_secrets` (workspace secrets injected as env). A non-public model's HF token is resolved
from `source.token_ref` and passed to the Pod as `HF_TOKEN`; it is masked in the recorded request.

## Verification

`tests/` and the `beam-pod-fake` conformance target run against fakes: an in-memory gateway REST
API (`httpx.MockTransport`), a fake SDK module driven by the real `launcher.deploy`, and a local
OpenAI-compatible server. `test_launcher.py` also runs `launcher.py` as a real child process
against a fake `beam` package. See `PLAN.md` for what ran against a real Beam workspace.
