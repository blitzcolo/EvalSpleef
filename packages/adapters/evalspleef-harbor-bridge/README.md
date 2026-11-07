# evalspleef-harbor-bridge

Bidirectional adapter between EvalSpleef and the Harbor evaluation framework (TECH-DESIGN section 9, PRD FR-ABS-07).

## Capabilities

1. **Direction 1 (Sandboxes -> Harbor Environment)**: `evalspleef_harbor_bridge.env:SpleefEnvironment` implements Harbor's `BaseEnvironment` interface, enabling any Harbor task to run on any EvalSpleef sandbox provider (e.g. `docker`, `novita-sandbox`, `hyperv`, `tart`).
2. **Direction 2 (Harbor as Executor)**: `evalspleef_harbor_bridge.runner:HarborRunner` (`slug: harbor`) runs Harbor tasks via external subprocess (`harbor run -p <task> -a <shell> -m <openai-compatible model string> -e evalspleef_harbor_bridge.env:SpleefEnvironment --ek provider=<instance>`) while keeping all model traffic routed to the gateway with trial key authentication.
3. **Event & Error Taxonomy**: `evalspleef_harbor_bridge.events:HarborAdapter` maps Harbor's event stream and error causes to EvalSpleef's `TrialMachine` events and core `ErrorClass` taxonomy.

## Verification status

**Not verified on a real host.**

Harbor is not installed in this repository's environment. The bridge implementation subclasses Harbor's `BaseEnvironment` dynamically when available and falls back to a documented interface shim.
All unit and integration tests run against:
- Shims of Harbor's `BaseEnvironment` and `ExecResult` with `MockSandboxProvider` underneath.
- A stub `harbor` executable created on `PATH` for subprocess invocation testing.

No test in this package has run against a real Harbor CLI or live Harbor framework installation.
