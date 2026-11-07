# evalspleef-runners

Agent runners for EvalSpleef (TECH-DESIGN section 8, PRD FR-RUN). A runner drives one
agent round: `start()` wires it to a sandbox, the gateway and a per-trial key,
`send()` runs one round (the task instruction or a feedback message) and returns a
`ShellTurn`, `stop()` tears the agent's process group down idempotently.

Registered through entry points in `pyproject.toml`:

| Slug | Class | Mode | Protocol |
| --- | --- | --- | --- |
| `terminus` | `unified_shell.UnifiedShellRunner` | `remote-tools` | `chat_completions` |
| `claude-code` | `native_shell.ClaudeCodeRunner` | `in-sandbox` | `anthropic_messages` |
| `codex-cli` | `native_shell.CodexCliRunner` | `in-sandbox` | `chat_completions` |
| `opencode` | `native_shell.OpenCodeRunner` | `in-sandbox` | `chat_completions` |
| `inspect` | `inspect_runner.InspectRunner` | `remote-tools` (no sandbox) | `chat_completions` |

## Unified shell (`terminus`, remote-tools)

The platform-owned agent loop runs outside the sandbox. Model calls go to the
gateway with the OpenAI Python client (`AsyncOpenAI(base_url=gateway_url,
api_key=trial_key, model=model_alias)`); tool calls enter the sandbox through the
`Sandbox` SPI, so the sandbox can stay fully offline.

Tools (function tools over chat completions):

- `bash(command, timeout_s)` → `sandbox.exec(command, cwd=/workspace, user=agent)`
  with the timeout bounded by the runner.
- `read(path)` → `sandbox.exec(["cat", path])`.
- `write(path, content)` / `edit(path, old, new)` → upload a staging file, then
  `mv` over the target for an atomic replace (`edit` requires exactly one match).
- `ask_user(question)` → ends the round with `need_clarification`; the question is
  the final message.

The loop never invents tool results: malformed tool-call JSON is returned to the
model as a tool error, and tool output is only what the sandbox produced.

Stop reasons (PRD appendix B.3): `completed` (assistant ends without a tool call),
`idle` (N consecutive empty assistant turns), `need_clarification`, `timeout`
(wall clock on an injected `Clock`), `cost_cap` / `token_cap` (gateway HTTP 429
with those error codes, or the runner's local counters against `RunnerBudget`),
`loop_detected` (the same tool call N times in a row), `crash` (unrecoverable
model/sandbox failure; details in `runner.last_error`), `cancelled`
(`runner.cancel()`).

Usage and cost: token usage is summed from the API usage fields; cost comes from
the `x-spleef-cost` response header when present, else 0. Every round appends to a
JSONL transcript (`ShellTurn.trace_ref`) with the trial key and any `sk-` shaped
literal redacted. The system prompt is neutral — no task id, no model name, no
mention of evaluation. A heartbeat fires every 15 s of clock time while a round
runs (PRD appendix B.3); pass `heartbeat=<callable>` to wire it to control-plane
reporting.

## Native shells (in-sandbox)

`NativeShellRunner.start()` uploads the generated CLI configuration into the
sandbox and starts the CLI in the background with a whitelisted environment
(`env -i` style: only `PATH`, `HOME` and the variables that specific CLI needs —
the per-trial key is the only credential, per FR-ISO-03). `send()` uploads the
round message and runs one turn of the CLI; the last stdout line
`SPLEEF_RESULT {"stop_reason": ..., "message": ...}` is parsed when present,
otherwise exit code 0 maps to `completed` (raw stdout as the final message) and a
non-zero exit to `crash`. `stop()` kills the CLI process group (the launcher uses
`setsid` where available, with a plain-pid fallback).

The serve/turn flags (`--serve` / `--turn`) are the runner's own protocol
abstraction for what real CLIs do interactively; **not verified against the real
CLIs**.

### Disabling web search, telemetry and auto-update

Setting names below were checked against the vendors' current documentation on
2026-11-07. They are **not verified against the real CLIs** — no sandbox image
with the real tools exists in this repository:

- **Claude Code** — env vars `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` /
  `ANTHROPIC_MODEL`; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (disables
  auto-updates, telemetry, error reporting), `DISABLE_AUTOUPDATER=1`;
  settings.json `disabledTools: ["WebSearch"]`.
  Sources: <https://code.claude.com/docs/en/env-vars>,
  <https://code.claude.com/docs/en/settings-reference>.
- **Codex CLI** — `~/.codex/config.toml` with `model_provider = "spleef"` and a
  `[model_providers.spleef]` entry (`base_url`, `env_key = "SPLEEF_KEY"`,
  `wire_api`); `web_search = false`, `check_for_update_on_startup = false`,
  `[analytics] enabled = false`, `[feedback] enabled = false`.
  Source: <https://developers.openai.com/codex/config-reference>.
  Note: current Codex docs list `wire_api = "responses"` as the only supported
  value; the assignment specifies `chat`, which is what we emit. Flagged as an
  open contract question.
- **OpenCode** — `opencode.json` with an `@ai-sdk/openai-compatible` provider
  (`options.baseURL`, `options.apiKey: "{env:SPLEEF_KEY}"`), `"share": "disabled"`
  (usage-data sharing), `"update": "disable"` (update checks) and
  `"permission": {"websearch": "deny", "webfetch": "deny"}`.
  Sources: <https://opencode.ai/docs/config/>,
  <https://opencode.ai/docs/providers/>.

## Inspect runner (logical track)

`InspectRunner` runs `inspect eval --model openai/<alias>` as a subprocess with
`OPENAI_BASE_URL` / `OPENAI_API_KEY` pointing at the gateway and
`INSPECT_LOG_DIR` set to a temporary directory; no sandbox is used (TECH-DESIGN
section 10). The newest `*.json` eval log in the log dir is parsed: `status`
(other than `success`) maps to `crash`, `usage` fields map to `TokenUsage`, and
the log's `message` becomes the final message. A non-zero exit maps to `crash`,
and the `deadline_s` bound maps to `timeout`.

## Verification status

- Unified shell: tested against `FakeOpenAIServer` (real HTTP) and
  `MockSandboxProvider` (real subprocesses in a temp dir) — tool loop, every stop
  reason, 429 error-code mapping, cost header, transcript redaction, heartbeat.
  Not verified against a real gateway or a real provider sandbox.
- Native shells: tested against a stub CLI script placed in a `MockSandboxProvider`
  sandbox. **Not verified against the real Claude Code, Codex CLI or OpenCode**;
  the documented setting names above are unverified assumptions until run on a
  template that has the real CLIs pinned.
- Inspect runner: tested against a stub `inspect` executable on `PATH`. **Not
  verified against real Inspect AI**; the log-parsing contract is our own
  minimal mapping.

## Tests

```bash
uv run pytest packages/evalspleef-runners
```

Tests are deterministic: time behaviour is driven by `FakeClock`, scripted
replies and bounded waits; there are no fixed sleeps or polling delays.
