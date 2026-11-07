# 01 · 系统架构

> 状态：v1 · 维护：Lead · 最后更新：2026-11-06
> 上游：[00-conventions](00-conventions.md) · 相关：[02-eval-flow](02-eval-flow.md) · [03-grading](03-grading.md) · [04-isolation](04-isolation.md) · [07-harness-mapping](07-harness-mapping.md)

## 1. 目标与非目标

**目标**

1. 一条可重复、可审计、可扩展的评测流水线，覆盖 Linux 容器、macOS VM、鸿蒙设备池、GPU 节点、裸机性能机五类执行环境。
2. 一个 trial 独占一个用完即毁的沙箱；trial 之间不存在可写共享物；隔离是否有效由金丝雀与体检报告**证明**，而不是口头声明。
3. 判分三层（脚本 / 裁判 / 人工）职责不重叠，档位与扣分可由确定性脚本聚合，人工只做标定与仲裁。
4. 一个批次（模型 × 题目 × epoch）跑完后产出榜单单元格 `扣分/档位(费用)`，同批附隔离体检与判定一致性报告。

**非目标**

- 不做模型训练、不做主观偏好排行；只评测"任务是否完成、完成得好不好、用了多少帮助"。
- 不在本仓库重写 Harbor / Inspect 本体；只做薄适配层、自研缺口组件与数据契约。
- 第一期不做实时榜单，T+1 批量出分即可；不做公网多租户 SaaS。

## 2. 系统全景

```
                       ┌───────────────────── 控制面（可信、内网）─────────────────────┐
   任务包 task.yaml ──▶ │ Task Registry ──▶ Batch Planner ──▶ Scheduler ──▶ Result Store │
                       │      │  lint/CI         │  计划冻结版本     │        ▲          │
                       │      ▼                  ▼                   ▼        │          │
                       │  Task Linter      Host/Device Inventory ◀── Sandbox Manager     │
                       └──────────────────────────────────┬───────────────────────────────┘
                                                          │ 租约（lease）+ 生命周期
   ┌────────────────────────────── 执行面（被测内容运行处，不可信）──────────────────────────────┐
   │  ┌───────────────┐  ┌──────────────────┐  ┌────────────────┐  ┌──────────────┐  ┌────────┐ │
   │  │ Linux 容器/VM │  │ macOS Tart VM    │  │ 鸿蒙设备池      │  │ GPU 节点容器 │  │ 性能机 │ │
   │  │ (Harbor 沙箱) │  │ 每 trial 克隆     │  │ 独占租用        │  │ 显存配额     │  │ 临时用户│ │
   │  └───────────────┘  └──────────────────┘  └────────────────┘  └──────────────┘  └────────┘ │
   │        ▲ SSH / ACP / hdc / xcodebuild（仅控制面发起，单向进入）                             │
   └───────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                               │
   ┌──────────────────────────── 外围（可信，均在沙箱外）──────────────────────────────────────────┐
   │ Agent Shell Runner ──▶ Model Gateway（计费/重试/供应商锁定）──▶ 被测模型 API                    │
   │ Observer（硬门槛/截图/日志/帧率/缺陷清单） ──▶ Feedback Controller（模拟用户 + 泄露检查）       │
   │ Verifier（独立验证沙箱 + 隐藏测试） ──▶ Judge Panel（多厂商裁判） ──▶ Human Console（仲裁）    │
   │ Isolation Auditor（金丝雀 / 相似度 / 轨迹审计 / 体检报告）                                     │
   └───────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                               │ 只追加事件日志 + 对象存储
                                        ┌──────┴───────┐
                                        │ Result Store │──▶ 榜单 / 批次报告 / 审计导出
                                        └──────────────┘
```

图中 **虚线框内为自研重点**：沙箱 provider 扩展、Observer、Feedback Controller、档位聚合、隔离审计。其余由 Harbor 提供或通过其扩展点接入，边界见 [07-harness-mapping](07-harness-mapping.md)。

## 3. 组件清单

| # | 组件 | 职责 | 关键输入 | 关键输出 | 来源 | 主要失败模式 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | Task Registry + Linter | 题目包注册、版本冻结、Schema/资源/预算/金丝雀校验 | `appendix/examples/*` | 不可变的 `task_version` 快照 | 自研（薄） | 题目包字段缺漏、隐藏测试混入 Agent 镜像 |
| C2 | Batch Planner | 生成 (模型 × 题目 × epoch) 单元格，冻结版本矩阵，估算成本与工期 | 榜单配置、可用资源 | `batch` + `cell` 计划 | 自研 | 版本混用、预算超募 |
| C3 | Scheduler | 按平台/资源/优先级排队与派发，处理租约、抢占、配额 | cell 计划、库存、租约 | trial 启动指令 | 自研（可复用 Harbor 调度） | 死锁、设备饥饿、槽位泄漏 |
| C4 | Sandbox Manager | 创建/销毁各平台沙箱，注入金丝雀，应用镜像 digest 与配额 | 平台类型、任务资源策略 | 沙箱句柄 + 隔离证据 | Harbor + 自研 provider | 起不来、镜像漂移、宿主污染 |
| C5 | Host / Device Inventory | 物理机、VM 槽位、鸿蒙设备、GPU、性能机的健康与占用 | 心跳、健康探测 | 可租资源池 | 自研 | 槽位不回收、设备掉线 |
| C6 | Agent Shell Runner | 在沙箱外启动各家 CLI 外壳，经 SSH/ACP 把工具调用落到沙箱 | 模型+外壳+指令+预算 | 轨迹事件、停止原因 | Harbor + 自研适配 | 外壳崩溃、挂起、版本漂移 |
| C7 | Model Gateway | 统一出站、计费、重试、限流、供应商锁定、响应头校验 | 模型请求 | 响应 + 用量 + 实际供应商 | 自研（LiteLLM 类网关可复用） | 中转降级、限流、密钥泄露 |
| C8 | Observer | 跑 L1 硬门槛、采截图/日志/帧率/无障碍树，产出结构化观察 | 沙箱、任务验收声明 | `observation` + 缺陷候选 | 自研 + Playwright/MCP | 观察超时、指标抖动、误报 |
| C9 | Feedback Controller | 按反馈阶梯决定下一轮是否反馈、用哪级、允许哪些字段 | 缺陷跟踪、历史轮次、预算 | `feedback` 决策 | 自研 | 泄露答案、升级过快/过慢 |
| C10 | Simulated User | 以用户口吻生成一条反馈，过泄露检查后发给 Agent | 反馈决策 + 现象目录 | 反馈文本 | 自研（调用 LLM） | 带入解法、语气失真 |
| C11 | Verifier | 独立验证沙箱中跑隐藏测试与性能验收 | 声明产物、隐藏测试 | 硬门槛结果 + 证据 | Harbor verifier | 测试不稳定、产物夹带 |
| C12 | Judge Panel | 原子 rubric 判分、多厂商评审团、k 次采样、分歧仲裁 | rubric + 证据 + 轨迹 | `judge_verdict` | Rewardkit + 自研编排 | 同家族偏好、注入攻击、解析失败 |
| C13 | Grader | 硬门槛短路、扣分累计、档位计算、费用聚合 | 观察+裁判+用量 | `grade` | 自研（参考实现见 `reference/evalkit`） | 规则歧义、重复计分 |
| C14 | Isolation Auditor | 金丝雀交叉扫描、产物相似度、作弊轨迹审计、体检报告 | 全部 trial 产物与轨迹 | 隔离报告 + 作废指令 | 自研 + `harbor analyze` | 漏扫、误判作废 |
| C15 | Human Console | 标定打标、仲裁队列、Perfect/Failed 边界复核 | 待仲裁样本 | 人工标签 | 自研（Harbor 查看器可复用） | 队列积压、标准漂移 |
| C16 | Result Store | trial 记录、事件日志、产物索引、榜单视图 | 全链路输出 | 榜单/报告/审计导出 | 自研 | 数据不一致、无法重放 |

## 4. 数据模型

| 实体 | 关键字段 | 关系 | 存储 |
| --- | --- | --- | --- |
| `task` / `task_version` | id、version、track、platform、budget、hard_gates、rubric_ref、status | 1—N version；version 冻结后不可变 | Postgres + 题目包快照到对象存储 |
| `batch` | id（`2026-11-b1`）、模型列表、外壳列表、epoch 数、预算上限、状态 | 1—N cell | Postgres |
| `cell` | batch、model、shell、task_version、epochs、状态（Pending/运行中/已出分/Pass/Skip） | 1—N trial | Postgres |
| `trial` | id、cell、epoch、state、validity、sandbox_lease、device_lease、开始/结束时间 | 1—N round / grade / artifact / canary | Postgres |
| `round` | trial、序号、起止、停止原因、用量 | 1—N observation / defect | Postgres |
| `observation` | round、类型（hard_gate/screenshot/log/perf/accessibility）、结果、证据 URI | N—1 round | 对象存储 + Postgres 索引 |
| `defect` | id（`R3#2`）、rubric_item、severity、首次发现轮、状态（new/open/persisting/fixed）、证据 | N—1 trial | Postgres |
| `feedback` | round、level、文本、泄露检查结果、生成模型 | 1—1 round | Postgres + 对象存储 |
| `judge_verdict` | rubric_item、judge、家族、采样序号、结论、置信/一致性、输入快照 URI | N—1 grade | Postgres |
| `grade` | trial、版本、tier、penalty、cost、hard_gate_summary、cheat_flag、人工覆盖 | 1—N（重判版本） | Postgres |
| `artifact` | trial、路径、sha256、大小、类型、导出许可 | N—1 trial | 对象存储 |
| `sandbox_lease` / `device_lease` | provider、host、快照/digest、租约起止、归还检查 | 1—1 trial | Postgres |
| `canary` | token、trial、埋点位置、扫描命中列表 | N—1 trial | Postgres |
| `incident` | 类型、关联 trial/host/device、处置、复盘链接 | N—1 trial/host | Postgres |
| `audit_event` | 事件 ID、时间、actor、type、payload、schema_version | 追加写 | JSONL（对象存储） |

**不变量**

- `task_version` 一旦被任何批次引用即不可修改，只能发新版本。
- `trial` 的 `state` 只能按 [02-eval-flow](02-eval-flow.md) 的合法转移表前进；重判产生 `grade` 新版本而不改 trial 状态。
- 每个 `trial` 恰好一条有效 `sandbox_lease`；涉及设备的任务恰好一条 `device_lease`。
- 所有 `grade` 必须能追溯到：task_version、model、shell、镜像 digest、题库版本、裁判版本、判分代码版本。

## 5. 存储与事件日志

- **Postgres**：状态与索引（trial/round/defect/grade 等），负责调度查询与榜单聚合。
- **对象存储（S3 兼容）**：轨迹原文、截图、日志、产物、裁判输入快照、金丝雀扫描证据。生命周期：原始产物保留 180 天，判分证据与最终榜单长期保留。
- **只追加事件日志（JSONL）**：每 trial 一条时间线，事件类型见 [02-eval-flow](02-eval-flow.md) 第 9 节；用于审计、重放与故障复盘。
- **重放能力**：给定 `trial` 记录 + 对象存储，可在不重跑 Agent 的前提下重算 `grade`（regrade）。这是判定迭代速度的关键。

## 6. 部署形态

- **控制面**：单 region 内网，K8s 部署 C1/C2/C3/C7/C9/C13/C14/C15/C16；无公网入站。
- **Linux 执行面**：Harbor + 容器/microVM provider（自建 Docker 主机，或 Daytona / Modal 等托管沙箱），按批次弹性扩缩。
- **Mac 执行面**：若干台 Apple Silicon 物理机跑 Tart；每台同时最多 2 个 macOS VM（[许可限制](https://pkg.go.dev/github.com/ysya/runscaler@v0.2.13)），Inventory 记录槽位并在槽位泄漏时摘除宿主。
- **性能机**：1 台高配 Mac 裸机，仅接受"已签名产物 + 临时用户"，不跑 Agent，不常驻服务。
- **GPU 节点**：Linux + 容器，显存与算力配额隔离，仅 Vulkan/渲染/推理类题使用。
- **数据面**：Postgres（主从）+ S3 兼容存储 + 只读榜单静态站点。

**容量估算公式**（用于批次排期）：

```
所需并发 = ceil(Σ(该平台 trial 数 × 平均时长) / 目标工期 × 安全系数 1.3)
trial 数 = 模型数 × 题目数 × epoch 数 × 重跑率(预计 5%~10%)
```

示例（假设）：8 个模型 × 30 题 × 3 epoch = 720 trial；其中 26 题在 Linux 容器（约 40 min/次），4 题在冷门平台（约 60 min/次）。

- Linux：26×8×3 = 624 trial × 40 min ≈ 416 h；若目标 12 h 完赛，需 ≈ ceil(416/12×1.3) ≈ 46 路并发。
- 冷门平台：4×8×3 = 96 trial × 60 min = 96 h；Mac 侧 8 路（4 台 × 2 VM）约 12 h，鸿蒙设备 8 台约 12 h。
- 结论：Linux 侧是弹性成本，Mac/鸿蒙侧是硬瓶颈；冷门题应错峰并接受更长周期。

## 7. 信任边界与安全

| 信任域 | 内容 | 对待方式 |
| --- | --- | --- |
| 可信控制面 | 调度器、网关、判分代码、隐藏测试、答案、API key | 常规工程安全；密钥只在此域 |
| 半可信执行面 | 沙箱宿主、Tart VM、设备池、GPU 节点 | 视为可能被弄脏；每 trial 重置；健康探测；隔离体检 |
| 不可信内容 | 被测 Agent 的代码/产物/网络请求、裁判输入中的产物文本 | 一律当数据；不执行、不信任、导出前过滤（见 [04-isolation](04-isolation.md)） |

密钥流转原则：模型 API key 仅存在于 Model Gateway；沙箱内无任何长期凭证；SSH/ACP 由控制面发起、单向进入；per-trial 短期令牌用完即废。任何 secret 进入沙箱即视为事故。

## 8. 自研 vs 复用的边界

| 能力 | Harbor/Inspect 提供 | 本项目自研 | 适配层 |
| --- | --- | --- | --- |
| 任务/沙箱/验证器生命周期 | ✅ | — | Task → Harbor task 映射 |
| 多外壳接入（Claude Code/Codex/OpenCode 等） | ✅ | 外壳版本矩阵 | Shell Adapter |
| 模拟用户多轮对话 | ✅（仅四种目标 Agent） | 反馈阶梯 + 泄露检查 | User Agent Adapter |
| 裁判（脚本/LLM/Agent 混合） | ✅ Rewardkit | 评审团编排、一致性统计 | Judge Adapter |
| macOS / 鸿蒙 / 性能机 | ❌ | ✅ 三类 provider | Harbor Sandbox Provider |
| 档位聚合与榜单 | ❌ | ✅ Grader + Report | 结果导出 |
| 金丝雀与隔离审计 | ❌（有 analyze） | ✅ Auditor | 轨迹/产物导出 |
| 错误分类与重试 | ✅ 单一错误策略表 | 本需求特有错误补充 | Error Policy 扩展 |

原则：**锁版本 + 薄适配**。所有 Harbor/Inspect 交互收敛到 `reference/evalkit/adapters/` 一个目录，升级只改这一层；版本号记录进每个 trial 的 `versions` 字段。

## 9. 质量属性与验证机制

| 属性 | 机制 | 验证手段 |
| --- | --- | --- |
| 可复现 | 镜像 digest、依赖版本、题目版本、随机种子、温度参数全部记录 | 同 trial 重跑档位差 ≤ 1 档的比例 ≥ 90% |
| 可审计 | 全轨迹、裁判输入快照、金丝雀报告、人工标签 | 任意单元格可下钻到证据 |
| 隔离有效 | 独立沙箱 + 金丝雀交叉扫描 + 相似度 | 每期体检报告零跨 trial 命中 |
| 判定一致 | 原子 rubric、多厂商评审团、标定 kappa | 与人工标签一致率 ≥ 阈值（[03-grading](03-grading.md)） |
| 成本可控 | 网关实时计费 + 每 trial/批次预算上限 | 批次费用误差 < 5% |
| 可扩展 | 题目包契约、Sandbox Provider 接口 | 新增一个平台 ≤ 2 周 |

## 10. 架构决策记录（ADR）

| # | 决策 | 理由 | 代价 | 备选 |
| --- | --- | --- | --- | --- |
| ADR-1 | 以 Harbor 为 Agentic 主干 | 模拟用户、独立验证、裁判、错误分类均为一等公民 | 锁版本、适配层维护 | 全自研（工期 +3 月） |
| ADR-2 | 验证在独立沙箱，只拷声明产物 | 防改测试/偷看答案；与 Harbor 默认一致 | 产物导出规则复杂 | 同沙箱验证（不可接受） |
| ADR-3 | 脑手分离：外壳在外、工具执行在内 | 沙箱可断网、密钥不进沙箱 | SSH 性能开销、适配复杂 | 沙箱内跑外壳（降隔离） |
| ADR-4 | 反馈由控制器 + 模拟用户生成，不由真人自由发挥 | 档位可复现、可自动化 | 需要预写现象目录与泄露检查 | 真人多轮（成本高） |
| ADR-5 | 档位由确定性脚本聚合，裁判只产原子结论 | 可测试、可解释、可重判 | 规则需持续标定 | 让 LLM 直接给档位（不可审计） |
| ADR-6 | 题库私有 + 参数化生成 + 月度滚动 | 防污染、防背题 | 出题成本高 | 公开固定题库（污染） |
| ADR-7 | 原生外壳与统一外壳双轨，榜单分栏 | 兼顾生态真实性与可比性 | 矩阵翻倍 | 只保留一种 |
| ADR-8 | 容器优先，冷门平台用专用 provider | 成本与并发最优 | 三套 provider 维护 | 全 VM（贵且慢） |

## 11. 架构验收标准

1. 任意 `trial` 记录能独立回答：用了哪个题目版本、哪个模型/外壳/镜像、哪几轮、每轮反馈级别、最终档位与扣分、费用、是否有效、隔离是否通过。
2. 任意 `grade` 可由 `observation + judge_verdict + 用量` 重新计算且结果一致（regrade 幂等）。
3. 新增一类沙箱 provider 只实现 5 个接口方法（provision / upload / exec / download / destroy），不改调度与判分代码。
4. 新增一道题只需新增一个题目包目录，不改任何代码；Linter 能拦截缺文件、超预算、隐藏测试泄入。
5. 批次级故障（宿主掉线、网关限流、设备掉线）不会污染有效成绩：受影响的 trial 标 `invalid` 并重跑，其余照常出分。
