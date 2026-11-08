# End-to-end batch on local Docker (success criterion 7)

Captured on 2026-11-08 by `pytest tests/e2e -m e2e` (exit 0, `1 passed in 10.78s`, run by the orchestrator after the grade fixes were committed). The test drives the real `spleef` command line in subprocesses against a real `spleef-gateway` process (two uvicorn servers, SQLite store), real local Docker containers and the in-repo fake model server.

## Result

- 3 task packs (logic-001, data-001, debug-001) x 2 models x 2 epochs = 12 trials, 6 cells, all scored; nothing is waiting for a human.
- The scripted reference agent reaches **Perfect** (0 deductions) on all three packs; the empty agent reaches **Failed** (28, 20 and 22 deductions).
- Every trial's canary scan is clean, every sandbox ran on `local-docker`, and the event log of every trial replays through the core state machine.
- For every trial the gateway's recorded usage matches the token counts in the runner transcript.
- No hard gate is waived; hash-locked inputs are never used as canary placements.

## What is real and what is not

- Real: the `spleef` commands, the gateway process and its trial keys and budgets, the Docker containers (read-only root, tmpfs workspace, non-root user), the pack hidden tests and checkers, the canary injection and scan, the grade computation.
- Fake: the model (a scripted fake OpenAI-compatible server: reference solution A for the reference agent, no tool calls for the empty agent) and the judge panel (a fake judge model). **Not verified against a real model or a real vendor endpoint.**
- Bridged, not fixed (follow-ups): the isolation preflight probes are prose in the adapters, so they are waived with a recorded reason in every preflight event; the gateway's public model name carries an extra `ep_` prefix that the command line strips when issuing keys; the verifier root is `/tmp/verify` because the read-only root filesystem has no `/verify`.
- Known limit: when a checker aborts early on an invalid answer, other items' violation codes look absent and pass; the trial still fails its hard gates and lands on tier F.

## Batch report

```text
judge panel: fake-judge
warm ep_fake-empty-agent_9af5eeb2: warm
warm ep_fake-ref-agent_e2a92f17: warm
retire ep_fake-empty-agent_9af5eeb2: retired
retire ep_fake-ref-agent_e2a92f17: retired

# 批次报告 2026-11-b1

> 规则：单元格格式 扣分/档位(费用)；费用取 median(cost.agent)；档位取 epoch 中位数；Pass 继承前代成绩。

- trial 数：12
- cell 数：6
- cell 状态：scored=6
- 人工复核队列：0 个 cell
- 生成时间：2026-11-08T00:30:21Z

## 榜单

| 题目 | fake-empty-agent | fake-ref-agent |
| --- | --- | --- |
| data-001 | 28/Failed(0.00) | 0/Perfect(0.00) |
| debug-001 | 20/Failed(0.00) | 0/Perfect(0.00) |
| logic-001 | 22/Failed(0.00) | 0/Perfect(0.00) |
```

## spleef config validate

```text
$ spleef config validate --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json --batch /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/batch.yaml
exit=0
--- stdout ---
models:    3 (fake-ref-agent, fake-empty-agent, fake-judge)
providers: 2 sandbox instance(s), 1 model account(s)
runners:   claude-code, codex-cli, harbor, inspect, opencode, scripted, terminus
tasks:     3 pack(s): data-001, debug-001, logic-001
batch:     2026-11-b1 (2 epoch(s))
config ok

--- stderr ---
```

## spleef models register

```text
$ spleef models register --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json
exit=0
--- stdout ---
fake-ref-agent@1  ep_fake-ref-agent_e2a92f17  verifying
fake-empty-agent@1  ep_fake-empty-agent_9af5eeb2  verifying
fake-judge@1  ep_fake-judge_c68c261c  verifying

--- stderr ---
```

## spleef models probe

```text
$ spleef models probe --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json
exit=0
--- stdout ---
fake-ref-agent  ep_fake-ref-agent_e2a92f17  probes 14/14 passed  report pr_9e4168fe5ff7dcc0
fake-empty-agent  ep_fake-empty-agent_9af5eeb2  probes 14/14 passed  report pr_496f67bd00b79342
fake-judge  ep_fake-judge_c68c261c  probes 14/14 passed  report pr_5e6b12cb3b9b75e6

--- stderr ---
```

## spleef batch plan

```text
$ spleef batch plan --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json --batch /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/batch.yaml
exit=0
--- stdout ---
data-001.fake-ref-agent.terminus	data-001	fake-ref-agent@1	terminus	ok (2 trial(s))
data-001.fake-empty-agent.terminus	data-001	fake-empty-agent@1	terminus	ok (2 trial(s))
debug-001.fake-ref-agent.terminus	debug-001	fake-ref-agent@1	terminus	ok (2 trial(s))
debug-001.fake-empty-agent.terminus	debug-001	fake-empty-agent@1	terminus	ok (2 trial(s))
logic-001.fake-ref-agent.terminus	logic-001	fake-ref-agent@1	terminus	ok (2 trial(s))
logic-001.fake-empty-agent.terminus	logic-001	fake-empty-agent@1	terminus	ok (2 trial(s))
cells: 6 runnable, 0 skipped; worst-case spend 88.00 USD (cap 100.00 USD)

--- stderr ---
```

## spleef batch freeze

```text
$ spleef batch freeze --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json --batch /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/batch.yaml
exit=0
--- stdout ---
batch 2026-11-b1 frozen
version matrix hash: sha256:0cc7e8fd768aaec526531d17bf3d3db87cc6ecfb3eae4aff66233e6389a48813
freeze record: /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/state/freezes/2026-11-b1.json

--- stderr ---
```

## spleef batch run

```text
$ spleef batch run --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json --batch /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/batch.yaml
exit=0
--- stdout ---
judge panel: fake-judge
warm ep_fake-empty-agent_9af5eeb2: warm
warm ep_fake-ref-agent_e2a92f17: warm
retire ep_fake-empty-agent_9af5eeb2: retired
retire ep_fake-ref-agent_e2a92f17: retired

# 批次报告 2026-11-b1

> 规则：单元格格式 扣分/档位(费用)；费用取 median(cost.agent)；档位取 epoch 中位数；Pass 继承前代成绩。

- trial 数：12
- cell 数：6
- cell 状态：scored=6
- 人工复核队列：0 个 cell
- 生成时间：2026-11-08T00:30:21Z

## 榜单

| 题目 | fake-empty-agent | fake-ref-agent |
| --- | --- | --- |
| data-001 | 28/Failed(0.00) | 0/Perfect(0.00) |
| debug-001 | 20/Failed(0.00) | 0/Perfect(0.00) |
| logic-001 | 22/Failed(0.00) | 0/Perfect(0.00) |

--- stderr ---
warning: S5 isolation probes waived for local-docker, mock-sandbox (the adapters ship prose probe descriptions; see the report)
```

## spleef batch report --json

```text
$ spleef batch report --json --models /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/models.yaml --providers /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/providers.yaml --tasks-dir /Users/pog/blitzcolo/EvalSpleef/tasks --templates-registry /Users/pog/blitzcolo/EvalSpleef/templates/registry.json --batch /private/var/folders/fh/5w07ff9j17v2z45bfnl686rh0000gn/T/pytest-of-pog/pytest-220/e2e0/config/batch.yaml
exit=0
--- stdout ---
{
  "batch_id": "2026-11-b1",
  "cell_count": 6,
  "cells": [
    {
      "best_tier": "F",
      "cell": "28/Failed(0.00)",
      "cell_id": "data-001.fake-empty-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-empty-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          28,
          28
        ],
        "epoch_tiers": [
          "F",
          "F"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-empty-agent",
      "penalty": 28,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "data-001",
      "tier": "F",
      "tier_display": "Failed",
      "tier_numeric": 0,
      "trial_count": 2,
      "worst_tier": "F"
    },
    {
      "best_tier": "P",
      "cell": "0/Perfect(0.00)",
      "cell_id": "data-001.fake-ref-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-ref-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          0,
          0
        ],
        "epoch_tiers": [
          "P",
          "P"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-ref-agent",
      "penalty": 0,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "data-001",
      "tier": "P",
      "tier_display": "Perfect",
      "tier_numeric": 7,
      "trial_count": 2,
      "worst_tier": "P"
    },
    {
      "best_tier": "F",
      "cell": "20/Failed(0.00)",
      "cell_id": "debug-001.fake-empty-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-empty-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          20,
          20
        ],
        "epoch_tiers": [
          "F",
          "F"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-empty-agent",
      "penalty": 20,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "debug-001",
      "tier": "F",
      "tier_display": "Failed",
      "tier_numeric": 0,
      "trial_count": 2,
      "worst_tier": "F"
    },
    {
      "best_tier": "P",
      "cell": "0/Perfect(0.00)",
      "cell_id": "debug-001.fake-ref-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-ref-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          0,
          0
        ],
        "epoch_tiers": [
          "P",
          "P"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-ref-agent",
      "penalty": 0,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "debug-001",
      "tier": "P",
      "tier_display": "Perfect",
      "tier_numeric": 7,
      "trial_count": 2,
      "worst_tier": "P"
    },
    {
      "best_tier": "F",
      "cell": "22/Failed(0.00)",
      "cell_id": "logic-001.fake-empty-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-empty-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          22,
          22
        ],
        "epoch_tiers": [
          "F",
          "F"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-empty-agent",
      "penalty": 22,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "logic-001",
      "tier": "F",
      "tier_display": "Failed",
      "tier_numeric": 0,
      "trial_count": 2,
      "worst_tier": "F"
    },
    {
      "best_tier": "P",
      "cell": "0/Perfect(0.00)",
      "cell_id": "logic-001.fake-ref-agent.terminus",
      "cheat": false,
      "cost_agent": 0.0,
      "display_label": "fake-ref-agent",
      "extensions": {
        "epoch_costs": [
          0.0,
          0.0
        ],
        "epoch_penalties": [
          0,
          0
        ],
        "epoch_tiers": [
          "P",
          "P"
        ]
      },
      "format_violation": false,
      "inherit_from": null,
      "manual_reasons": [],
      "manual_review": false,
      "model": "fake-ref-agent",
      "penalty": 0,
      "shell": "terminus",
      "skip_reason": null,
      "status": "scored",
      "task_id": "logic-001",
      "tier": "P",
      "tier_display": "Perfect",
      "tier_numeric": 7,
      "trial_count": 2,
      "worst_tier": "P"
    }
  ],
  "generated_at": "2026-11-08T00:30:22Z",
  "review_queue": [],
  "trial_count": 12
}

--- stderr ---
```
