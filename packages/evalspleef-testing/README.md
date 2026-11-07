# evalspleef-testing

Programmable test doubles for the EvalSpleef core packages. They implement the SPIs in
`evalspleef-spi` and replace external systems (sandbox vendors, model endpoints, the model
gateway, agents, secret stores) with deterministic, inspectable objects. Everything here is
test-only: it performs no network calls except the loopback fake server, and it never reaches a
real vendor. Runs against these doubles are **not verified on a real host**.

Install it as a workspace dependency (`evalspleef-testing`), import modules directly, and inject a
`FakeClock` wherever a double accepts a `clock` argument.

## `clock`

`Clock` is the protocol, `SystemClock` the real implementation, and `FakeClock` a deterministic
clock whose time moves only through `advance`; `sleep` suspends until `advance` passes the
deadline. Use it to make lifetimes, cold starts and expiry testable without wall time.

```python
from evalspleef.testing.clock import FakeClock

clock = FakeClock(start=1000.0)
# ... pass clock into a provider; advance instead of sleeping
clock.advance(60.0)
```

## `mock_sandbox`

`MockSandboxProvider` runs each "sandbox" as a host temp directory plus real subprocesses
(`/bin/sh -c` for strings, direct argv for lists, whole-process-group kill on timeout).
`MockSandbox` implements exec/upload/download/destroy with path-escape checks, output
truncation, streaming callbacks and permission preservation. The provider adds capability and
network-policy validation, quota, orphan listing/reaping, and failure injection through
`fail_next_creates`, `lose` and `set_quota`. `sandbox.root` exposes the host directory for
assertions; `destroyed` and `lost` report lifecycle state.

```python
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

provider = MockSandboxProvider("mock-1", {}, DictSecretResolver(), clock=FakeClock())
sandbox = await provider.create(spec)  # a SandboxSpec from evalspleef-spi
result = await sandbox.exec("pytest -q")
await sandbox.destroy()
```

## `mock_endpoint`

Three `ModelEndpointProvider` doubles cover the management modes: `MockExternalProvider`
(register and health-check an existing URL), `MockAssistedProvider` (deploy in a console, then
`attach` with operator input) and `MockApiProvider` (all lifecycle operations apply immediately).
They validate configuration, plan deployments, track endpoint state, expose the gateway
`UpstreamRoute`, and use the injected clock for cold starts.

```python
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_endpoint import MockApiProvider
from evalspleef.testing.secrets import DictSecretResolver

provider = MockApiProvider(
    "acct", {"base_url": "https://mock.invalid/v1", "model": "mock-32b"}, DictSecretResolver()
)
plan = await provider.plan(model_spec)
handle = await provider.deploy(model_spec, plan)
route = provider.upstream(handle)
```

## `fake_openai`

`FakeOpenAIServer` is a real HTTP (`FastAPI`/`uvicorn`) OpenAI-compatible server on an ephemeral
loopback port. Queue exact replies with `queue_reply` (content, tool calls, reasoning), inject
HTTP errors with `inject_error`/`inject_error_for`, and inspect every call through `requests`.
Without a queued reply it derives an echo-style answer, supports streaming with usage chunks,
JSON schemas and tool-call payloads, and enforces the configured API key and context limit.

```python
import httpx
from evalspleef.testing.fake_openai import FakeOpenAIServer

with FakeOpenAIServer(model="fake-model") as server:
    server.queue_reply("done")
    response = httpx.post(
        f"{server.base_url}/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
    )
assert response.json()["choices"][0]["message"]["content"] == "done"
```

## `fake_gateway`

`InMemoryGatewayAdmin` is the gateway management surface backed by dictionaries: routes, prices,
trial keys, revocation and usage. `issue_key` mints a `TrialKey`; `authenticate` resolves a secret
and raises `TrialKeyRejected` for unknown, revoked or expired keys; `record_usage` costs a call
from the frozen price and stamps it with the clock. `spent_usd`, `tokens_used` and
`budget_status` give cheap assertions.

```python
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin

admin = InMemoryGatewayAdmin(clock=FakeClock(start=1_700_000_000.0))
await admin.upsert_route("ep_qwen", upstream_route)
await admin.set_price("ep_qwen", price_spec)
key = await admin.issue_key(
    trial_id="t1",
    endpoint_id="ep_qwen",
    max_budget_usd=1.0,
    max_tokens=100_000,
    expires_at="2023-11-15T00:00:00+00:00",
)
admin.record_usage(trial_id="t1", round=0, route_name="ep_qwen", input_tokens=10, output_tokens=5)
```

## `secrets`

`DictSecretResolver` resolves references that are literal keys of an in-memory mapping;
`EnvSecretResolver` resolves `env://NAME` against an injected environment (or `os.environ`).
Both read at resolve time and keep nothing on disk. Unknown references raise `KeyError`;
malformed environment references raise `ValueError`.

```python
from evalspleef.testing.secrets import DictSecretResolver, EnvSecretResolver

secrets = DictSecretResolver({"vault://hf/eval-readonly": "hf-token"})
assert secrets.resolve("vault://hf/eval-readonly") == "hf-token"
assert EnvSecretResolver({"NOVITA_API_KEY": "n1"}).resolve("env://NOVITA_API_KEY") == "n1"
```

## `scripted_runner`

`ScriptedRunner` is a deterministic `AgentRunner`. Each `send` consumes the next `ScriptedRound`:
it runs the round's shell commands and file writes against the sandbox, records the
`ExecResult`s, and returns the configured `ShellTurn` (stop reason, final message, steps, usage,
cost, duration). An empty script is the empty agent; running past the last round completes with
zero steps.

```python
from evalspleef.spi.runner import RunnerBudget, StopReason
from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner

runner = ScriptedRunner(
    [
        ScriptedRound(
            commands=("pytest -q",),
            files={"notes.txt": "fixed"},
            stop_reason=StopReason.COMPLETED,
            final_message="done",
        ),
    ]
)
await runner.start(
    sandbox,
    gateway_url="http://gateway.test/v1",
    trial_key="sk-spleef-test",
    model_alias="ep_qwen",
    instruction="make the test pass",
    budget=RunnerBudget(max_total_tokens=100_000, max_cost_usd=1.0, max_wall_clock_s=600),
)
turn = await runner.send("feedback F1", deadline_s=60)
```

## Entry points

The package registers `mock-sandbox` under `evalspleef.sandbox_providers`, `mock-external`,
`mock-assisted` and `mock-api` under `evalspleef.model_providers`, and `scripted` under
`evalspleef.runners`, so a registry can discover the doubles by name.
