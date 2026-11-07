# evalspleef-sbx-k8s-agent-sandbox

Kubernetes sandbox provider (`k8s-agent-sandbox`) for EvalSpleef, built on the
[kubernetes-sigs/agent-sandbox](https://agent-sandbox.sigs.k8s.io/) custom
resources. One `Sandbox` custom resource per trial, isolated by a gVisor or Kata
`RuntimeClass`, with egress restricted by a generated Cilium FQDN policy.
Implements the sandbox SPI (TECH-DESIGN section 7.4.8).

## Verification status

**Not verified on a real host.** Every test and the conformance target run
against an in-memory `FakeKubeApi`; no Kubernetes cluster, CRD, controller,
RuntimeClass installation or Cilium CNI was available. The `RealKubeApi` code
paths (kubernetes Python client) are written but unexercised, except that
importing the package and constructing the provider do not import `kubernetes`
(the extra is loaded lazily).

## Entry points

- `evalspleef.sandbox_providers`: `k8s-agent-sandbox = evalspleef_sbx_k8s_agent_sandbox.provider:K8sAgentSandboxProvider`
- `evalspleef.conformance_targets`: `k8s-agent-sandbox = evalspleef_sbx_k8s_agent_sandbox.conformance:build_targets`

The conformance target is named `k8s-agent-sandbox-fake` and sets `fake=True`.

## CRD contract (checked 2026-11-07)

| Resource | Group / version | Fields used |
| --- | --- | --- |
| `Sandbox` | `agents.x-k8s.io/v1beta1` | `spec.podTemplate` (`metadata.labels`, `spec.runtimeClassName`, `spec.containers`, `spec.volumes`, `spec.securityContext`, `spec.nodeSelector`, `spec.hostname`, `spec.automountServiceAccountToken`), `spec.shutdownPolicy`, `spec.shutdownTime`, `status.conditions[type=Ready]` |
| `SandboxTemplate` | `extensions.agents.x-k8s.io/v1beta1` | `spec.podTemplate`, `spec.volumeClaimTemplates` |
| `SandboxWarmPool` | `extensions.agents.x-k8s.io/v1beta1` | `spec.replicas`, `spec.sandboxTemplateRef.name` |
| `SandboxClaim` | `extensions.agents.x-k8s.io/v1beta1` | `spec.warmPoolRef.name`, `spec.lifecycle.shutdownPolicy`, `spec.lifecycle.shutdownTime` |
| `CiliumNetworkPolicy` | `cilium.io/v2` | `spec.endpointSelector`, `spec.egress[].toFQDNs`, `spec.egress[].toEndpoints`, `spec.egress[].toPorts` |

Sources:

- <https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/main/k8s/crds/agents.x-k8s.io_sandboxes.yaml>
- <https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/main/clients/python/agentic-sandbox-client/python-sandbox-template.yaml>
- <https://agent-sandbox.sigs.k8s.io/docs/sandbox/lifecycle/>
- <https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/gvisor/>
- <https://github.com/kubernetes-sigs/agent-sandbox/blob/main/docs/api-migration-guide.md>
- <https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/demo-cilium-egress/>

Agent-sandbox names the pod after the Sandbox (or after the claim, see the
upstream `SandboxClaim` example), which is what `exec` targets. The provider
resolves the `Sandbox` object by the claim name in warm-pool mode.

## Provider configuration

`K8sAgentSandboxProvider(instance, config, secrets, *, api=None, clock=None)`
reads:

| key | default | meaning |
| --- | --- | --- |
| `namespace` | `default` | namespace for sandboxes, claims and egress policies |
| `runtime_class` | `gvisor` | `gvisor`/`runsc` => isolation L2, `kata-*` => L3; anything else is rejected |
| `warm_pool` | unset | `SandboxWarmPool` name; when set, create claims an instance instead of creating a `Sandbox` |
| `max_concurrency` | 16 | live sandboxes; excess `create` calls raise `QuotaExceeded` |
| `max_lifetime_s` | 7200 | capability ceiling for `spec.max_lifetime_s` |
| `ready_timeout_s` | 120 | maximum wait for the Sandbox `Ready` condition |
| `ready_poll_s` | 0.5 | readiness poll interval |
| `enforces_limits` | `["cpu", "memory"]` | declared `enforces_limits`; the fake target passes `[]` because it cannot enforce cgroups |
| `context` / `kubeconfig` | in-cluster, else default kubeconfig | kubernetes client context |

The optional `k8s` extra installs the `kubernetes` client:
`uv sync --all-packages --all-extras` or `pip install evalspleef-sbx-k8s-agent-sandbox[k8s]`.

## Behaviour

- **create**: validates the spec against `capabilities()`. Unsupported OS,
  template kind, GUI, GPUs, lifetime, CIDR allowlists, `via_proxy` or hostnames
  on non-allowlist modes raise `CapabilityNotSupported` / `PolicyNotEnforceable`
  before any object is written. Labels from `spec.labels` are copied verbatim
  (invalid Kubernetes labels are rejected) plus `spleef.provider`,
  `spleef.created_at` and `spleef.expires_at`. The pod runs non-root, read-only
  root filesystem, `capabilities.drop: [ALL]`, no service-account token,
  `RuntimeDefault` seccomp, neutral hostname `sandbox`, and `emptyDir` volumes at
  `/workspace` and `/tmp`. `shutdownPolicy: Delete` bounds the instance lifetime.
- **warm pool (I14)**: a warm-pool instance is claimed through a `SandboxClaim`
  and is never returned; `destroy` deletes the claim and its Sandbox. There is
  no return-to-pool operation.
- **egress**: `off` writes a `CiliumNetworkPolicy` with an empty egress list
  (default-deny); `docs_whitelist`/`package_mirror` write a DNS rule plus one
  `toFQDNs` rule per hostname on ports 80/443; `full` writes no policy.
  `allowlist_kinds` is `{"hostname"}` only.
- **exec**: runs through pod exec with streamed stdout/stderr, stdin, explicit
  working directory and env. Timeouts wrap the command in GNU
  `timeout --kill-after=5s` inside the pod and return `exit_code=124` with
  `timed_out=True`; a client-side watchdog closes the stream after
  `timeout_s + 15s`. Output above `max_output_bytes` is truncated head-and-tail.
- **files**: upload/download use tar streams over pod exec and apply I11
  sanitisation on download (no escaping symlinks, special files, setuid bits,
  size caps).
- **list/reap**: `list` filters `Sandbox` objects by label; `reap` deletes
  matches older than `older_than_s` or past `spleef.expires_at`, together with
  their egress policy and claim.

## Isolation profile and probes

`isolation_profile.yaml` and `isolation_profile()` carry the complete I1-I16
profile (no waivers) for the declared runtime class. `probes/linux.sh` is the
Linux preflight probe set; `K8sAgentSandboxProvider.run_probe(sandbox)` feeds it
to the sandbox through `exec`. The tests keep the YAML and Python profile in
sync.

## Conformance

```bash
uv run pytest -p evalspleef.conformance.plugin \
    --spleef-provider k8s-agent-sandbox-fake \
    --spleef-report /tmp/spleef-k8s-agent-sandbox-fake-report.json \
    packages/adapters/evalspleef-sbx-k8s-agent-sandbox -q
```

The target uses `FakeKubeApi`, `fake=True`. Network observer cases (C-NET-01,
C-NET-02) are not applicable because no `reachable_from_sandbox` observer is
claimed; C-NET-05, C-SNAP-01/02, C-GUI-01 and C-PORT-01 are not applicable by
capability declaration. C-ISO-05 is not applicable because the fake target
declares no enforced limits, and C-CONC-01 is skipped for fake targets. C-NET-03
(unsupported policy rejected) and C-NET-04 (metadata address unreachable) do run
against local subprocesses.

## Known limitations and unverified areas

- Nothing was run against a real Kubernetes cluster, the agent-sandbox
  controller, a gVisor/Kata RuntimeClass or Cilium. The CRD group/version and
  field names were read from the upstream manifests cited above.
- Real exec assumes GNU coreutils `timeout` exists in the task image; without it
  a timed-out command exits 127 and the remote process is only bounded by the
  client watchdog, which cannot kill it. This is not verified.
- Real download uses the kubernetes websocket client, which decodes output as
  UTF-8; binary artifacts may be corrupted on the real path. The fake path uses
  direct file copies and is binary-safe.
- `upload`/`download` require `tar` and `mkdir` in the task image.
- `exec`'s `user` argument cannot switch users in a pod; the account comes from
  the pod security context (`run_as_user` when numeric).
- Cilium FQDN policies require the Cilium CNI with DNS proxy enabled; clusters
  without Cilium fail at Sandbox creation.
