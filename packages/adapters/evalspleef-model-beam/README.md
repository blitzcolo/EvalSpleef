# evalspleef-model-beam

Model provider `beam-pod` (management mode `api`): an OpenAI-compatible SGLang server for a
pinned Hugging Face revision, deployed as a [Beam](https://www.beam.cloud) Pod with a stable,
token-protected URL. All inference traffic still goes through the gateway; the gateway calls the
Pod URL with the Beam token as its upstream key.

## How it talks to Beam

| Operation | Surface |
| --- | --- |
| `deploy` | with `weights_volume` set, first a CPU-only Pod run (`Pod(...).create()`) puts the pinned revision into the Volume and is polled to `COMPLETE` through `GET /api/v1/task/{ws}/{task}`; then `launcher.py` in a child process: the Beam SDK (`beam-client`) builds the registry image and runs `Pod(...).deploy(name)`, then `POST /deployments/{id}/scale` starts the planned container count |
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

## Weights from a Beam Volume

Set `weights_volume` (for example `huggingface_models`) to keep the weights in a Beam Volume
instead of downloading them in every GPU container:

1. A CPU-only Pod (`fetch_cpu` 2, `fetch_memory` `8Gi`, the engine image by default) mounts the
   Volume at `weights_mount` (default `/<volume>`) and runs `huggingface_hub.snapshot_download`
   for the pinned revision into `<mount>/<repo>`, the layout Beam's dashboard import uses. Files
   whose recorded commit already matches are skipped, so a cached revision costs only metadata
   checks; other content is replaced by the pinned revision. Repo, revision and directory reach
   the Pod through its environment, never through command text. The run must reach `COMPLETE`
   within `fetch_timeout_s` (3600); an `ERROR`, `CANCELLED` or `TIMEOUT` task, or a run past the
   deadline (its container is stopped), raises `LaunchFailed` and no GPU Pod is started.
2. The GPU Pod mounts the same Volume and SGLang loads `--model-path <mount>/<repo>`; it gets no
   HF token.

The HF token: name a Beam workspace secret in `hf_token_secret` (for example `HUGGINGFACE_TOKEN`,
which the dashboard import creates) and the token never leaves Beam; otherwise the token from
`source.token_ref` is passed to the weights Pod only, masked in the record.

Volume storage is not billed yet; the documented planned price is $0.0001 per GB-hour
(<https://docs.beam.cloud/v2/data/volume.md>), about $1.8 a month for 25 GB.

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
`beam_secrets` (workspace secrets injected as env), `default_params` (request defaults the
gateway route applies, e.g. `{chat_template_kwargs: {enable_thinking: false}}`; account config
may set it too, the spec wins), and the Volume keys above (`weights_volume`,
`weights_mount`, `hf_token_secret`, `fetch_cpu`, `fetch_memory`, `fetch_timeout_s`; account config
may set `fetch_image`, `fetch_poll_s`, `rest_base`). Without a Volume a non-public model's HF
token is resolved from `source.token_ref` and passed to the GPU Pod as `HF_TOKEN`; it is masked
in the recorded request.

## Multi-GPU layouts

`gpu_count` goes up to 8 (one container). Without explicit sizes the adapter adds `--tp-size
<gpu_count>`. A preset may set the layout itself with `--tp-size`, `--pp-size`, `--dp-size`,
`--ep-size` and `--enable-dp-attention`; `validate` and `plan` refuse it before any GPU is billed
unless tp x pp x dp equals the GPU count (with DP attention: tp x pp equals it and dp divides tp)
and ep divides tp. People choose layouts; the adapter only checks that they fit. `validate` also
reads the workspace and refuses a multi-GPU launch when the workspace has multi-GPU containers
disabled or allows fewer GPUs than requested. On 2026-11-08 the workspace used here refused a
2-GPU container, so multi-GPU layouts are **not verified on a real host**.

## Verification

`tests/` and the `beam-pod-fake` conformance target run against fakes: an in-memory gateway REST
API (`httpx.MockTransport`), a fake SDK module driven by the real `launcher.deploy`, and a local
OpenAI-compatible server. `test_launcher.py` also runs `launcher.py` as a real child process
against a fake `beam` package. See `PLAN.md` for what ran against a real Beam workspace.
