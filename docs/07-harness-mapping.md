# 07 · Harbor / Inspect 落地映射与适配层

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[00-conventions](00-conventions.md) · 相关：[01-architecture](01-architecture.md)、[02-eval-flow](02-eval-flow.md)、[03-grading](03-grading.md)、[04-isolation](04-isolation.md)、`docs/05`（tasks-a 维护）、[调研 01 Harbor 核实](../research/01-harbor-verification.md)、[99 事实核查](../research/99-factcheck-plan-v1.md)
> 版本基准：Harbor **v0.25.0**（2026-10-25 发布，PyPI 同日）、harbor-rewardkit **0.2.4**、Inspect AI **0.3.287**（2026-10-31）。本文件所有 Harbor 行为均核对到 2026-11-06 的文档/源码，证据见[调研 01](../research/01-harbor-verification.md)。

## 1. 目标与非目标

**目标**：让工程师照着本文就能把 `appendix/examples/*` 的题包、`reference/evalkit` 的流程实现与 Harbor/Inspect 两个上游对接起来——包括概念映射、文件与字段映射、需要自研的薄适配层接口、CLI 命令、版本锁定与升级迁移。

**非目标**：不重新解释 grading 规则（见 [03](03-grading.md)）、不重复隔离威胁模型（见 [04](04-isolation.md)）、不定义题目内容（见 `docs/05`（tasks-a 维护））。

**一句话架构**：**Harbor 做执行与验证的骨架，Inspect 做逻辑赛道的题面/评分壳，evalkit 做"档位 + 反馈阶梯 + 评审团 + 金丝雀"这层本项目特有逻辑**；三者之间只通过本文件定义的 7 个适配接口通信，不互相 import 内部模块。

```
                    ┌──────────────────────────────────────────────┐
                    │  调度层（我们）：状态矩阵 / 预算 / 批次        │
                    │  reference/evalkit.state.TrialMachine        │
                    └───────────────┬──────────────────────────────┘
             ┌──────────────────────┼───────────────────────────┐
             ▼                      ▼                           ▼
   ┌──────────────────┐   ┌──────────────────┐        ┌──────────────────┐
   │ Harbor（执行/验证）│   │ Inspect（逻辑赛道）│        │ 自研 provider     │
   │ task/trial/sandbox│   │ Task/Solver/Scorer│        │ macOS(Tart)/鸿蒙   │
   └────────┬─────────┘   └────────┬─────────┘        └────────┬─────────┘
            │ 7 个适配接口（本文 §4）│                            │
            ▼                      ▼                            ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ evalkit：Grade / JudgePanel / FeedbackLadder / DefectTracker /    │
   │          CanaryRegistry / 榜单聚合                                 │
   └──────────────────────────────────────────────────────────────────┘
```

## 2. 概念映射表

| Harbor 概念（证据：<https://docs.harborframework.com/llms.txt>） | 本项目组件 | 现有产物 | 处置 |
| --- | --- | --- | --- |
| **task**（`task.toml` + `instruction.md` + `environment/` + `tests/` + `solution/`） | 题包 `appendix/examples/<task-id>/`（[task.yaml](../schemas/task.schema.json) + instruction.md + persona.md + rubric.yaml + defects.yaml + hidden-tests/ + answers/ + canary.md） | 6 个示例题包 | **自研格式，转译成 Harbor task**（§3） |
| **trial**（一次 agent×task×epoch 执行，产出 `result.json`） | `trial`（[trial.schema.json](../schemas/trial.schema.json)，ID 规则见 00-conventions §2） | evalkit `TrialRecord` | Harbor trial 是**执行事实**，我们的 trial 是**评测事实**：一对一，用 `extensions.harbor_trial_id` 关联 |
| **verifier**（`tests/test.sh` → `/logs/verifier/reward.json`） | L1 硬门槛 + `hidden-tests/run.sh` | `docs/06`（tasks-a 维护） | **复用**：把 `hidden-tests/run.sh` 包成 `test.sh`，输出我们的 `hard_gate_summary` |
| **Rewardkit judge criteria**（`tests/*.toml`，binary/likert/numeric + samples + guard） | L2 单条 rubric 判分（`check.kind=llm_judge/agent_judge`） | [03](03-grading.md)、`reference/evalkit/judge.py` | **复用 judge 调用**，**自研面板聚合**（Rewardkit 一次 run 只有一个 judge） |
| **simulated user**（`--user-agent/--user-model/--bridge acp`） | 模拟用户 + F0–F3 反馈阶梯 | `appendix/personas/`、evalkit `FeedbackLadder` | **自研控制器**，Harbor 只负责多轮通道 |
| **sandbox / environment**（`BaseEnvironment` + `EnvironmentCapabilities`） | 沙箱 provider（container/Tart/鸿蒙设备池） | `reference/evalkit/canary.py`、[04](04-isolation.md) | **复用 docker/daytona/modal/gke**，**自研 macOS/鸿蒙 provider** |
| **artifacts**（`artifacts=[...]`，验证前收集并拷进 verifier） | 声明的产物 + 证据（截图/日志/帧时间） | 题包 `evidence/` | **复用**，并在 `task.yaml.artifacts` 里声明 |
| **error classification**（8 个 `FailureCause`，`reward_valid/retry/grade`） | `error.class` + `error.disposition`（00-conventions §4.6） | `reference/evalkit/models.py` `ErrorClass/Disposition` | **映射**（§6），不合并枚举 |
| **regrade**（`harbor job regrade` 用新 verifier 重跑判定） | 重判 → `trial.grades[]` 新版本 | [02](02-eval-flow.md) | **复用**为"重判执行器" |
| **multi-step**（`[[steps]]` + `min_reward` 早停） | 反馈阶梯的"每轮验收" | [02](02-eval-flow.md) | **部分复用**：Harbor step 是固定脚本序列，我们的轮次由控制器动态决定，**不直接用 multi-step 表达反馈轮** |
| **analyze**（agent 读轨迹标 reward hacking / task spec） | 作弊审查 | [04](04-isolation.md) | **复用**，并加自定义 rubric |
| **lint**（静态规则） | 题包 CI 检查 | `docs/05`（tasks-a 维护） | **复用**，规则集见 §7.3 |
| **Inspect Task/Dataset/Solver/Scorer** | 逻辑赛道（`track=logic`） | 示例 `logic-001`、`logic-002` | **复用**（§8） |
| **Inspect log viewer** | 人工复核界面 | L3 人工 | **复用**，与 `harbor view` 二选一 |

## 3. 文件与字段映射（题包 → Harbor task）

转换脚本放在 `scripts/`（由 impl/tasks-a 维护），约定输入 `appendix/examples/<task-id>/`，输出 `build/harbor-tasks/<task-id>/`。

| 我们的文件/字段 | Harbor 目标 | 转换规则 |
| --- | --- | --- |
| `task.yaml` | `task.toml` | 字段级映射见下表 |
| `instruction.md` | `instruction.md` | 原样拷贝；**不写实现提示**（题包已约定） |
| `persona.md` | `<build>/persona.md` | 通过 `--user-persona-path` 传给 Harbor，不塞进 task |
| `hidden-tests/run.sh` | `tests/test.sh` | 包一层：`bash /tests/run.sh`，把结果转成 `/logs/verifier/reward.json`（`{"hard_gate": 0|1, ...}`） |
| `hidden-tests/**` | `tests/**`（或打进 verifier 镜像） | separate verifier 时优先 `tests/Dockerfile` 烘进去 |
| `answers/reference-a.*` | `solution/solve.sh`（oracle） | 用于 `harbor run -a oracle` 自检 |
| `answers/empty-agent.md` | 无 | 我们的自检：空 agent 必须 Failed，用 `-a nop` 跑（Harbor 内置 `nop` agent） |
| `canary.md` | 无 | 由外置脚本注入（金丝雀不是 Harbor 概念） |
| `resources.image` / `image_digest` | `[environment].docker_image`（带 `@sha256:`） | 必须固定 digest，否则 lint 报 `unpinned-image` |
| `resources.cpu / memory_mb / disk_mb / gpu` | `[environment].cpus / memory_mb / storage_mb / gpus` | `gpus` 只在 provider 声明 GPU 能力时有效 |
| `network.mode` ∈ {`off`,`docs_whitelist`,`package_mirror`,`full`} | `[agent].network_mode` ∈ {`no-network`,`allowlist`,`public`} | `off→no-network`；`docs_whitelist/package_mirror→allowlist`；`full→public` |
| `network.allowlist` | `[agent].allowed_hosts` | 只支持域名/IP/CIDR，**不支持路径或方法级规则**；"只读镜像、屏蔽 publish"要在代理层做 |
| `network.blocklist` | — | Harbor 只有 allowlist 语义；黑名单必须走自建代理 |
| `verification.entry` | `tests/test.sh` 的入口 | 同上 |
| `artifacts` | 顶层 `artifacts = [...]` | 路径必须是容器内绝对路径 |
| `budget.max_rounds / max_wall_clock_s` | `[agent].timeout_sec` 等 | 轮数预算 Harbor 不管，由控制器执行 |
| `budget.max_cost_usd` | 无 | 由**网关计费**执行；Harbor 只记录 token/费用字段 |
| `hard_gates` | `tests/test.sh` 的退出码 + reward | 硬门槛失败 → reward 0；映射表见 `docs/06`（tasks-a 维护） |
| `rubric_ref`/`rubric` | `tests/*.toml`（Rewardkit） | 由 `rubric.yaml` 生成准则；**只有** `check.kind=llm_judge/agent_judge` 的条目进 TOML |
| `stop_conditions` | — | 我们的控制器用；Harbor 用 `min_reward` 表达"提前终止" |
| `canary.locations` | — | 注入脚本按清单埋令牌 |

### 3.1 生成的 `task.toml` 骨架（可执行示例）

```toml
# build/harbor-tasks/web-001/task.toml
# 由 scripts/task_to_harbor.py 从 appendix/examples/web-001/task.yaml 生成，勿手改

artifacts = ["/app/dist", "/app/evidence"]

[environment]
docker_image = "registry.local/llm-eval/web-001@sha256:1111111111111111111111111111111111111111111111111111111111111111"
cpus = 4
memory_mb = 8192
storage_mb = 20480
network_mode = "no-network"          # 来自 network.mode=off

[agent]
timeout_sec = 3600
user = "agent"                        # 强制非 root（Harbor 不是默认，见调研 01 H9）
network_mode = "allowlist"            # 来自 network.mode=docs_whitelist
allowed_hosts = ["developer.mozilla.org", "developer.apple.com"]

[verifier]
environment_mode = "separate"         # 强制独立验证沙箱（新建任务默认，但老任务不是）
timeout_sec = 900

[verifier.environment]
docker_image = "registry.local/llm-eval/web-001-verifier@sha256:2222222222222222222222222222222222222222222222222222222222222222"
network_mode = "no-network"
cpus = 2
memory_mb = 4096

[metadata.harbor.lint]
extend-select = ["root-agent", "unpinned-image"]
```

> 说明：`[agent].user` 在当前 Harbor 是 **opt-in**（默认不建 agent 用户）；`[verifier] environment_mode` 对 `harbor init` 新建任务是默认，但**历史任务默认 shared**，所以模板里显式写。两条都来自 <https://docs.harborframework.com/news/reward-hacking.md>（快照 2026-11-06）。

## 4. 适配层：`evalkit/adapters.py`（本项目唯一新增的 Python 层）

设计原则：**只用标准库 + `typing.Protocol`/ABC**；上层（控制器、榜单）只依赖这些接口，Harbor/Inspect 的升级被隔离在实现类里；每个接口都对应一个"可替换实现"：Harbor 实现、Inspect 实现、Mock 实现（测试用）。

### 4.1 接口总览

| 接口 | 责任 | Harbor 对应 | 自研实现 |
| --- | --- | --- | --- |
| `SandboxProvider` | 创建/销毁沙箱、能力声明 | `BaseEnvironment.type()/start()/stop()` | macOS(Tart)、鸿蒙设备池 |
| `SandboxLease` | 一次 trial 独占的沙箱句柄 | `BaseEnvironment` 实例 | 设备租约（真机） |
| `AgentShell` | 驱动被测 Agent 外壳（一轮对话/一次运行） | Harbor agent 集成（`-a claude-code` 等） | 统一外壳 Terminus/OpenCode 包装 |
| `SimulatedUser` | 按反馈级别生成用户话术并过泄露检查 | simulated user 的 persona+template | ★核心自研 |
| `Observer` | 观测产物：截图/日志/帧时间/测试结果 → 证据与缺陷候选 | 无 | ★核心自研 |
| `Verifier` | 硬门槛执行，产出可对账的 reward | `tests/test.sh` + Rewardkit | 复用 + 包装 |
| `JudgeBackend` | 单条 rubric 的单裁判调用 | Rewardkit judge / Inspect scorer | 复用 |
| `JudgePanel` | 多厂商裁判聚合、一致性 | 无 | ★已有 `evalkit/judge.py` |
| `ResultStore` | 落 trial/grade/轨迹（供 regrade 与审计） | `result.json` + `artifacts/manifest.json` | 复用 + 扩展字段 |

### 4.2 接口签名（可直接落地）

```python
"""evalkit.adapters — 上游框架（Harbor/Inspect）与 evalkit 之间的薄适配层。

约束：
- 只用 Python 标准库；
- 所有外部对象（Harbor 环境、Inspect 实例）只以不透明句柄出现，
  不让 Harbor/Inspect 的类型出现在本模块的公开签名里；
- 每个接口都提供 Mock 实现，单元测试不依赖网络与容器。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

from .models import Budget, Defect, ErrorClass, Evidence, FeedbackLevel, Grade, TrialRecord


# ---------------------------------------------------------------- 沙箱提供方
@dataclass(frozen=True)
class SandboxCapabilities:
    """与 Harbor EnvironmentCapabilities 对齐的最小集合（字段名保持可映射）。"""

    disable_internet: bool = False
    network_allowlist_hostnames: bool = False
    network_allowlist_wildcard_hostnames: bool = False
    dynamic_network_policy: bool = False   # 支持 [agent]/[verifier] 逐阶段切换
    gpus: bool = False
    stream: bool = False
    mounted: bool = False
    windows: bool = False


@dataclass(frozen=True)
class SandboxSpec:
    """一次 trial 的沙箱规格；字段来自 task.yaml.resources/network。"""

    task_id: str
    trial_id: str
    image: str
    image_digest: str | None
    cpus: int
    memory_mb: int
    disk_mb: int
    gpus: int = 0
    gpu_types: tuple[str, ...] = ()
    os: Literal["linux", "macos", "harmony", "windows"] = "linux"
    network_mode: Literal["off", "docs_whitelist", "package_mirror", "full"] = "off"
    allowed_hosts: tuple[str, ...] = ()
    #: provider 私有参数（Harbor 的 --ek）；不同实现自行解释
    provider_kwargs: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SandboxLease(Protocol):
    """一个 trial 独占的沙箱。生命周期由控制器负责（创建→使用→销毁）。"""

    trial_id: str

    def exec(self, command: str, *, cwd: str | None = None, env: dict[str, str] | None = None,
             timeout_sec: int | None = None, user: str | int | None = None) -> tuple[int, str, str]:
        """返回 (returncode, stdout, stderr)。语义对齐 Harbor BaseEnvironment.exec。"""

    def upload_file(self, source: Path, target: str) -> None: ...
    def download_file(self, source: str, target: Path) -> None: ...
    def set_network_policy(self, mode: str, allowed_hosts: Iterable[str] = ()) -> None:
        """只在 capabilities.dynamic_network_policy=True 时允许调用。"""


class SandboxProvider(abc.ABC):
    """沙箱提供方。Harbor 侧通过 BaseEnvironment 子类 + `-e module:Class` 注册；
    自研 macOS/鸿蒙 provider 同时实现本类（供 evalkit 直接调度）与 Harbor 子类（供 harbor run）。
    """

    name: str

    @abc.abstractmethod
    def capabilities(self) -> SandboxCapabilities: ...

    @abc.abstractmethod
    def acquire(self, spec: SandboxSpec) -> SandboxLease:
        """创建沙箱；失败必须抛 SandboxStartError（→ error.class=environment）。"""

    @abc.abstractmethod
    def release(self, lease: SandboxLease, *, delete: bool = True) -> None: ...


class SandboxStartError(RuntimeError):
    """映射 Harbor 的 sandbox_failed（reward 无效、可重试、不判分）。"""


# ---------------------------------------------------------------- Agent 外壳
@dataclass(frozen=True)
class ShellTurn:
    """一次"让 Agent 干活"的输入输出。"""

    round_index: int
    message: str                       # 本轮用户消息（F0 时为中性开场）
    transcript_ref: str                # 轨迹文件相对路径
    stop_reason: str                   # "finished" | "asked" | "timeout" | "crashed" | "budget"
    usage: dict[str, int]              # {"input_tokens":..., "output_tokens":..., "cost_usd_micro":...}


class AgentShell(abc.ABC):
    """被测 Agent 外壳（原生外壳或统一外壳）。一个 trial 一个实例。"""

    name: str

    @abc.abstractmethod
    def start(self, lease: SandboxLease, *, instruction_ref: str, env: dict[str, str]) -> None: ...

    @abc.abstractmethod
    def send(self, message: str, *, deadline_s: int) -> ShellTurn: ...

    @abc.abstractmethod
    def stop(self) -> None:
        """幂等；被杀掉的子进程要在这里回收（Harbor 的 reward-hacking 公告要求清进程组）。"""


# ---------------------------------------------------------------- 模拟用户
@dataclass(frozen=True)
class TurnContext:
    """模拟用户能看到的一切。泄露检查的边界就是这里的字段集合。"""

    round_index: int
    level: FeedbackLevel
    allowed_fields: frozenset[str]
    defect_digest: tuple[dict[str, Any], ...]   # 已按 allowed_fields 过滤
    persona_id: str
    history: tuple[str, ...]


class SimulatedUser(abc.ABC):
    """把"缺陷清单 + 反馈级别"翻译成一句人话，并保证不泄露。"""

    @abc.abstractmethod
    def next_message(self, ctx: TurnContext) -> str:
        """返回已经通过 LeakChecker 的文本；不允许直接返回模型原始输出。"""

    @abc.abstractmethod
    def check_leak(self, text: str) -> tuple[bool, tuple[str, ...]]:
        """(是否安全, 命中的泄露类别)。对应 reference/evalkit/feedback.py 的 LeakChecker。"""


# ---------------------------------------------------------------- 观察器
@dataclass(frozen=True)
class ObservationBundle:
    """一次观测的结果：证据 + 缺陷候选 + 硬门槛快照。"""

    evidence: tuple[Evidence, ...]
    defects: tuple[Defect, ...]
    hard_gates_passed: bool
    raw_refs: dict[str, str] = field(default_factory=dict)   # {"screenshot": "...", "log": "..."}


class Observer(abc.ABC):
    """"给 Agent 一样的眼睛"：截图、读日志、驱动浏览器、采帧时间。"""

    @abc.abstractmethod
    def observe(self, lease: SandboxLease, *, round_index: int, artifacts: Iterable[str]) -> ObservationBundle: ...


# ---------------------------------------------------------------- 验证器
@dataclass(frozen=True)
class VerifierResult:
    """对齐 Harbor 的 reward.json 语义，但保留我们的硬门槛明细。"""

    hard_gate_summary: dict[str, Any]
    reward: float
    detail_refs: dict[str, str] = field(default_factory=dict)
    verifier_version: str = "0.0.0"


class Verifier(abc.ABC):
    @abc.abstractmethod
    def verify(self, *, trial: TrialRecord, artifacts: Iterable[Path], verifier_env: SandboxLease | None) -> VerifierResult:
        """独立验证沙箱：只读声明的产物；不得读 agent 工作区的其他文件。"""


# ---------------------------------------------------------------- 裁判
class JudgeBackend(Protocol):
    """单裁判后端。Harbor 实现 = rewardkit 的 [judge]；Inspect 实现 = model_graded_*。"""

    def grade_criterion(self, *, criterion_id: str, prompt: str, files: Iterable[Path]) -> tuple[bool, float]:
        """返回 (裁决, 置信度/一致性分值)。超时必须抛 JudgeTimeoutError。"""


class JudgeTimeoutError(RuntimeError):
    """映射 Harbor 的裁判超时语义：该准则记 0 但必须留痕，不能静默 0。"""


# ---------------------------------------------------------------- 结果存储
class ResultStore(Protocol):
    """trial/grade/轨迹的持久化。Harbor 的 result.json 是其中一个视图。"""

    def save_trial(self, trial: TrialRecord) -> None: ...
    def save_grade(self, grade: Grade) -> None: ...
    def load_artifacts(self, trial_id: str) -> list[Path]: ...
```

实现落位（与现有代码对齐，不新建平行体系）：

| 现有模块 | 与适配层的关系 |
| --- | --- |
| `reference/evalkit/judge.py`（`JudgeClient`/`JudgePanel`/`MockJudge`） | `JudgeBackend` 是它的单裁判输入面；`JudgePanel` 保留（面板聚合、`agreement`） |
| `reference/evalkit/feedback.py`（`FeedbackLadder`/`LeakChecker`） | `SimulatedUser` 的内部实现就是"Ladder 选级别 + 模板生成 + LeakChecker 校验" |
| `reference/evalkit/defects.py`（`DefectTracker`） | `Observer` 输出的 `defects` 直接进 `DefectTracker` 去重/跟踪 |
| `reference/evalkit/grading.py`（`compute_grade`） | `VerifierResult` + `FeedbackRecord` + `Defect` 是 `compute_grade` 的输入 |
| `reference/evalkit/state.py`（`TrialMachine`） | `SandboxLease`/`AgentShell` 的调用时机由状态机事件驱动（§5） |
| `reference/evalkit/canary.py`（`CanaryRegistry`） | `SandboxProvider.acquire` 之后注入令牌；`release` 之后扫描 |

## 5. 生命周期：Harbor 阶段 ↔ 我们的状态机

我们的 trial 状态：`pending → leased → running → observing → (awaiting_feedback → running)* → verifying → scored`（枚举见 [00-conventions](00-conventions.md) §4.1，转移表见 [02](02-eval-flow.md)）。映射：

| 我们的状态 | 谁在动 | Harbor 对应动作 | evalkit 接口 |
| --- | --- | --- | --- |
| `pending` | 调度器 | —（未创建 trial） | — |
| `leased` | 调度器 | `SandboxProvider.acquire` ≈ `BaseEnvironment.start()` | `SandboxProvider` |
| `running` | Agent 外壳 | `agent.run()`；simulated-user 里是 target 侧 | `AgentShell.send` |
| `observing` | 观察器 + L1 脚本 | Harbor 验证前 run logs/artifacts 收集 | `Observer.observe` + `Verifier.verify`（可见部分） |
| `awaiting_feedback` | 控制器 | `--user-agent` 那条消息（Harbor 内部无独立阶段） | `SimulatedUser.next_message` |
| `verifying` | 独立验证沙箱 | `verifier.verify()` + Rewardkit judge | `Verifier` + `JudgeBackend` |
| `scored` | 聚合器 | `result.json` 已写 | `compute_grade` + `ResultStore` |
| 重判 | 聚合器 | `harbor job regrade`（不重跑 agent） | 新 `Grade` 版本，`supersedes` 旧版本 |
| `invalid` | 审计 | Harbor `reward_valid=false` 的 cause | 见 §6 |

**关键差异（必须写进实现）**：Harbor 的 trial 是"一条直线"（agent → verifier），我们的 trial 是**带反馈循环**的。所以：
1. 控制器在 `awaiting_feedback` 与 `running` 之间循环 N 次，**这层循环在 Harbor 之外**，用同一个 sandbox lease 保持工作区；
2. Harbor 的 `multi-step` **不是**这个循环的替代品（它是固定 steps 的脚本序列 + `min_reward` 早停）；
3. 循环期间的预算（轮数/墙钟/费用）由控制器记账，超出则 `budget_exhausted` 事件进 verifying。

## 6. 错误分类映射（Harbor cause ↔ 我们的 error.class）

Harbor 8 个 cause（源码 `src/harbor/models/trial/error_policy.py`，文档 <https://docs.harborframework.com/jobs/error-classification.md>）→ 我们的 8 类（[00-conventions](00-conventions.md) §4.6）：

| Harbor `failure_cause` | `reward_valid` | 本项目 `error.class` | `error.disposition` | 备注 |
| --- | --- | --- | --- | --- |
| `agent_exhausted_budget` | ✅ | `model` | `score` | 成绩有效、按当时产物定档；若预算耗尽导致未满足成功条件（硬门槛未全过或有未修复 functional/fatal），按 G-BUDGET-F 判 F，并带 `budget_exhausted` 标记（[03](03-grading.md) §9.6）；参考项目的 Failed 是能力语义，与本方案不同，跨榜对照时注意 |
| `agent_refused_task` | ✅ | `model` | `score` | 单独统计拒答率 |
| `sandbox_failed` | ❌ | `environment` | `retry` | 重试上限内仍失败 → `discard` |
| `model_provider_failed` | ❌ | `provider` | `retry`（鉴权类 → `manual`） | 鉴权/NotFound 不 grade |
| `harbor_failed` | ❌ | `environment` | `manual` | 框架自身故障，需人工看 |
| `verifier_failed` | ❌ | `verification` | `retry`/`manual` | `RegradeError` 走 manual |
| `unclassified` | ❌ | `environment` | `retry` | 先重试一次再定 |
| `cancelled` | ❌ | `environment` | `discard` | 人工取消 |
| —（Harbor 无此概念） | — | `cheat` | `zero` | 由 `harbor analyze` + 金丝雀扫描产出 |
| — | — | `isolation` | `discard` | 金丝雀跨 trial 命中 |
| — | — | `judge` | `manual` | 一致率低于阈值/裁判超时 |
| — | — | `version` | `manual` | 镜像/外壳/模型版本漂移 |

落库字段：`trial.error.harbor_cause`（新增扩展字段，放 `extensions.harbor_cause`）+ `trial.error.class` + `trial.error.disposition`。**不合并两套枚举**，因为 Harbor 的 cause 会随版本增删，而我们的 `error.class` 是对外口径。

## 7. 复用 / 自研判定表

### 7.1 直接复用（不改上游代码）

| 能力 | 用 Harbor 的什么 | 前置条件 |
| --- | --- | --- |
| 容器沙箱与 GPU 调度 | `docker` / `daytona` / `modal` / `gke` provider | 装对应 extra；写清 capability |
| 网络三模式与逐阶段切换 | `no-network/allowlist/public` + `[agent]/[verifier]` | provider 声明 `dynamic_network_policy`（docker 需要 egress sidecar） |
| 独立验证沙箱 | `environment_mode="separate"` + artifacts | 产物必须先 materialize 成文件（进程内状态不算） |
| 单裁判调用 | Rewardkit `[[criterion]]` + `samples` + `guard` | judge TOML 由 rubric.yaml 生成 |
| 多步/早停 | `[[steps]]` + `min_reward` | 只用于"固定里程碑"的题，不用于反馈循环 |
| 重判 | `harbor job regrade` | trial 有 `result.json` + `artifacts/manifest.json` |
| 作弊审查 | `harbor analyze`（自定义 rubric） | 需要 LLM key；结论回写 `error.class=cheat` |
| 静态 lint | `harbor lint --extend-select ...` | 规则集见 §7.3 |
| 轨迹查看/分享 | `harbor view` / `harbor upload` | 公开与否是政策问题（[plan §9.3](../drafts/plan-v1.md)） |
| 逻辑题面与评分 | Inspect `Task/Dataset/Scorer` | 见 §8 |

### 7.2 必须自研（工作量大头）

| 自研件 | 为什么上游没有 | 交付物 |
| --- | --- | --- |
| `SimulatedUser` + F0–F3 控制器 | Harbor 的 simulated user 是**自由对话**，没有"按缺陷清单选字段、防泄露、升级规则" | `appendix/personas/` + `evalkit/feedback.py` + 适配器实现 |
| `Observer`（截图/帧时间/日志/浏览器驱动） | Harbor 只提供 MCP 挂载点，不自带 GUI 观察 | `appendix/examples/*/evidence/` + MCP server + 适配器 |
| `JudgePanel`（跨厂商评审团） | Rewardkit 一次 run 只有 1 个 judge | `evalkit/judge.py` 已有；补"至少 2 家、不同家族"约束 |
| 档位聚合器（含半档、Failed 语义） | 参考项目私有规则；Harbor 只有 reward 数值 | `evalkit/grading.py` |
| macOS(Tart) provider | Harbor 无 macOS VM provider | `sandbox_tart.py`（实现 `BaseEnvironment` + `SandboxProvider`） |
| 鸿蒙设备池 provider | 无 | `sandbox_harmony.py` + 设备租约/归还清理 |
| 金丝雀注入与跨 trial 扫描 | Harbor 无 | `evalkit/canary.py` |
| 榜单/批次聚合 | Harbor Hub 榜单是它的形态 | 本项目报表（[02](02-eval-flow.md)） |

### 7.3 CI 里跑的 lint 规则集（固定）

```bash
# 1) 默认规则（含 shared-verifier、agent-image-test-leak、verifier-network-download、compose-privileged 等）
harbor lint build/harbor-tasks --json > out/lint.json
# 2) 默认关闭但我们必开的两条
harbor lint build/harbor-tasks --extend-select unpinned-image --extend-select root-agent
# 3) 题目质量评审（agent 按 rubric 读题）
harbor check build/harbor-tasks/<task-id> --rubric appendix/grading-rubric.md
```

规则证据：<https://docs.harborframework.com/tasks/lint.md>（`unpinned-image`、`root-agent` 默认 `—`）。`harbor check` 见 <https://docs.harborframework.com/news/reward-hacking.md>。

## 8. 逻辑赛道：Inspect AI 接入

**为什么用 Inspect**：逻辑题（28 题 × 270 用例的形态）本质是"单轮到多轮问答 + 过程判定"，Inspect 的 `Dataset/Solver/Scorer` 分离 + `model_graded_qa` 正好；Harbor 的多轮反馈循环在这里是过度设计。证据：<https://inspect.aisi.org.uk/llms.txt>、scorers 页 <https://inspect.aisi.org.uk/scorers.html>。

### 8.1 组件映射

| Inspect 概念 | 本项目 | 落地方式 |
| --- | --- | --- |
| `Task` | 一个 logic 题包 | `@task` 函数，`dataset=...`、`solver=...`、`scorer=...` |
| `Dataset/Sample` | 题目实例（参数化生成器产物） | `Sample(input=题面, target=参考解/判分要点, metadata={task_id, version})` |
| `Solver` | 被测模型（+ 可选多轮试探） | 内置 `generate()`；多轮题用 `use_tools` 或自定义 solver |
| `Scorer` | 答案比对 + 推导判定 | `exact()/f1()` 判最终答案；`model_graded_qa()` 判推导是否覆盖关键中间结论 |
| `epochs` | 每题跑 3 次（对齐参考项目"3 Pass 取最高/取中位"） | `--epochs 3`，聚合时同时算 max 与 median |
| `Log Viewer` | L3 人工复核 | `inspect view` |
| `inspect_k8s_sandbox` | 大规模隔离执行（可选） | 仅当逻辑题需要隔离执行时 |

### 8.2 评分桥接

Inspect 的 scorer 输出 `Score`（value ∈ [0,1] + explanation）→ 我们的判定：

```text
answer_score ∈ {0,1}            → hard_gate.answer_ok
derivation_pass ∈ {0,1}         → rubric R1（推导成立）/ R2（是否枚举猜测）
格式违规（正则先筛 + 裁判复核）  → 命中即 0 分（参考项目规则）
tier = 由 answer/derivation/格式 三元组按 docs/03 规则映射；与 Agentic 赛道共用 grade.schema.json
```

**注意**：Inspect 的 `model_graded_qa` 是**单裁判**；本项目的"评审团 + 一致性"仍由 `evalkit.JudgePanel` 承担——把 Inspect scorer 的结果当作其中一个 `JudgeBackend.grade_criterion` 的返回。

### 8.3 命令示例

```bash
# 固定版本安装（见 §9）
uv tool install "inspect-ai==0.3.287"

# 跑一题三 epoch，结果落 logs/
inspect eval evalkit_inspect/logic_tasks.py@logic_001 \
  --model anthropic/claude-sonnet-5 \
  --epochs 3 \
  --log-dir logs/inspect/2026-11-b1 \
  --max-connections 4

# 只跑参数化生成器的某几个种子
inspect eval evalkit_inspect/logic_tasks.py@param_gen -T seed=1,2,3 -T n_items=10

# 离线复核
inspect view --log-dir logs/inspect/2026-11-b1
```

## 9. 版本锁定与升级迁移

### 9.1 锁定矩阵（写进 `constraints.txt` + 镜像 digest）

| 组件 | 锁定值 | 依据/位置 |
| --- | --- | --- |
| Python | `>=3.12`（Harbor 要求） | harbor `pyproject.toml` `requires-python` |
| Harbor | `harbor==0.25.0`（patch 可跟 `0.25.x`） | PyPI 2026-10-25 |
| Harbor 可选 extra | `harbor[daytona]` 等显式列出 | 预集成 provider 文档 |
| Rewardkit | `harbor-rewardkit==0.2.4` | PyPI |
| Inspect AI | `inspect-ai==0.3.287` | PyPI 2026-10-31 |
| Agent 外壳 | 每个 `AgentShell` 固定版本（Claude Code / Codex CLI / OpenCode / Kimi Code） | 外壳自身发布页；版本号记进 `trial.versions` |
| 模型 | 网关侧固定 provider+模型 slug；禁止中转路由漂移（网关记录实际厂商） | [plan §8](../drafts/plan-v1.md) 服务商行 |
| 题目镜像 | `image@sha256:...`（双镜像：agent + verifier） | `task.yaml.resources.image_digest` |
| 逻辑题 scorer 模板 | 与 `rubric_version` 一起版本化 | `grade.rubric_version` |

```bash
# constraints.txt（唯一允许的版本来源）
harbor==0.25.0
harbor-rewardkit==0.2.4
inspect-ai==0.3.287
# provider extras 由安装脚本追加：--extra daytona --extra modal
uv pip install -c constraints.txt "harbor[daytona]" "harbor-rewardkit" "inspect-ai"
```

### 9.2 升级迁移协议

| 变更级别 | 判定 | 动作 | 结果复用 |
| --- | --- | --- | --- |
| PATCH（题目文案/资源） | 语义不变 | 直接换题包版本 | 复用旧 trial（Harbor `--diff`） |
| MINOR（verifier/验收变化） | 需要新判分 | `harbor job regrade <job> -p <new-task>` | 复用 agent 轨迹，仅重判 |
| MAJOR（验收语义/环境变化） | 需要重跑 | 新一期批次 | 不复用，榜单标"跨期不可比" |
| Harbor 大版本（0.25→0.26） | 上游行为可能变 | 先在 canary job 上跑 10 个题，比对 `result.json` 字段与 reward；再滚动 | 旧 trial 保留但用旧版本判分记录 |
| Inspect 升级 | scorer 行为可能变 | 固定 `--epochs` 与 scorer 模板版本，重跑 5 题做一致性对比 | 逻辑题重新标定 |

**回滚**：`constraints.txt` + 镜像 digest + `git tag` 三位一体；任一升级前先打 tag，升级后 canary 不过即回滚。

### 9.3 版本字段落库（必填，供跨期比较）

```text
trial.versions = {
  harbor, rewardkit, inspect_ai, python,
  shell_name, shell_version, model_slug, model_provider, gateway,
  task_id, task_version, image_digest_agent, image_digest_verifier,
  persona_version, rubric_version, verifier_version,
  fx_rate, currency="USD"      # 榜单展示参考项目数据时用 CNY，见调研 99 L11
}
```

## 10. CLI 速查（本项目日常操作）

```bash
# 0) 题包 → Harbor task
python3 scripts/task_to_harbor.py appendix/examples/web-001 --out build/harbor-tasks

# 1) 自检：oracle 解必须过、nop 必须失败
harbor run -p build/harbor-tasks/web-001 -a oracle -m anthropic/claude-sonnet-5 -e docker
harbor run -p build/harbor-tasks/web-001 -a nop -e docker          # 期望 reward=0

# 2) 正式 trial（原生外壳；模拟用户 + 反馈阶梯走我们控制器时不用 --user-agent）
harbor run -d "llm-eval-web@2026-11" -a claude-code -m anthropic/claude-sonnet-5 \
  -e daytona -n 32 --job-name 2026-11-b1-web

# 3) Harbor 原生模拟用户（只用于对照实验，不用于正式档位）
harbor run -p build/harbor-tasks/web-001 \
  --agent claude-code --model anthropic/claude-sonnet-5 \
  --user-agent claude-code --user-model anthropic/claude-sonnet-5 \
  --user-persona-path appendix/personas/web-001-persona.md \
  --bridge acp

# 4) 静态检查 / 题目质量 / 作弊审查
harbor lint build/harbor-tasks --extend-select unpinned-image --extend-select root-agent
harbor check build/harbor-tasks/web-001 --rubric appendix/grading-rubric.md
harbor analyze jobs/2026-11-b1-web --rubric appendix/cheat-rubric.md --agent codex --model openai/gpt-6-luna

# 5) 重判 / 查看 / 分享
harbor job regrade jobs/2026-11-b1-web -p build/harbor-tasks -e docker
harbor view jobs
harbor upload jobs/2026-11-b1-web        # 默认 private

# 6) 逻辑赛道（evalkit_inspect/ 由 impl 提供）
inspect eval evalkit_inspect/logic_tasks.py@logic_001 --model anthropic/claude-sonnet-5 --epochs 3

# 7) 本项目聚合（evalkit；cli.py 由 impl 交付后可用，未交付前直接 import 函数）
python3 -c "from evalkit.grading import compute_grade; print('ok')"
python3 -m evalkit grade --trial jobs/... --rubric appendix/examples/web-001/rubric.yaml
python3 -m evalkit leaderboard --batch 2026-11-b1 --format markdown
```

## 11. 验收标准（本文件自身可检验）

1. 任何题包都能被 `scripts/task_to_harbor.py` 转成通过 `harbor lint`（含 `--extend-select unpinned-image`）的 task。
2. `-a oracle` 全过、`-a nop` 全 0；两条命令写进 CI。
3. 每个 trial 的 `versions` 字段齐全（§9.3），缺字段视为无效 trial。
4. `error.class` 能由 `harbor_cause` 唯一映射（§6 表全覆盖，新增 cause 时 CI 报警）。
5. `judge_panel` 至少 2 个不同厂商；`agreement` 低于阈值进人工队列。
6. 适配层单元测试不依赖网络与容器（全部 Mock 实现）。

**TODO(run-on-host)**（每处都必须有 -> 说明）：

1. `harbor run -e daytona ...` 的实际 preflight 与 `--ek` 取值：需在有 Daytona key 的宿主机跑一次（本文只核对文档）。-> `TODO(run-on-host): archive a real run log under out/preflight/`
2. `scripts/task_to_harbor.py` 产物对 Harbor v0.25.0 schema 的校验（需宿主机装 harbor 后跑 `harbor lint`）。
3. Tart/鸿蒙 provider 的 `BaseEnvironment` 子类注册（`-e my_sandbox:MySandbox`）与 capability 拒绝路径：需 macOS 宿主实测。-> `TODO(run-on-host): tart smoke test`

## 12. 对方案的影响

1. **plan-v1 §3 的"trial 闭环"要拆成"Harbor 直线 + 控制器循环"两层**：Harbor 负责单次 agent→verify，反馈循环在 Harbor 之上；这样 regrade/analyze/lint 都能直接复用。
2. **题包模板必须新增 3 个强制字段**（`[agent].user`、`[verifier] environment_mode`、`[agent] network_mode`），否则"默认安全"的假设不成立（见[调研 01](../research/01-harbor-verification.md) §7）。
3. **多裁判面板与观察器是自研件，工期要按自研估**：Harbor 只给单裁判与 MCP 挂载点。
4. **逻辑赛道用 Inspect 不代表双轨数据**：两条轨道的最终产物都进 `grade.schema.json`，榜单聚合器只认 grade，不认上游框架。
5. **版本治理要落到 `constraints.txt` + 镜像 digest + `versions` 字段**：Harbor 约 3–4 周一个版本，regrade 与 `--diff` 是压低重跑成本的唯一杠杆。
