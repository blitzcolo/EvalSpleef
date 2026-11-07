# 00 · 项目约定与目录契约

> 状态：v1 · 维护：Lead · 最后更新：2026-11-06
> 本文是全仓库的**唯一命名与结构基准**。子文档、JSON Schema、示例题、参考实现若与本文冲突，以本文为准；确需修改本文，先给 Lead 发消息。

## 0. 项目是什么

本项目把一份战略级方案（[`drafts/plan-v1.md`](../drafts/plan-v1.md)，原始文件在用户 Downloads）落成可执行、可校验的工程规格。五个支柱与文档的对应关系：

| 支柱 | 主文档 | 配套产物 |
| --- | --- | --- |
| 评测流 | [`docs/02-eval-flow.md`](02-eval-flow.md) | `schemas/trial.schema.json`、`reference/evalkit/` 状态机 |
| 题目 | [`docs/05-task-library.md`](05-task-library.md) | `schemas/task.schema.json`、`appendix/examples/*/task.yaml` |
| 答案 | [`docs/06-answer-and-acceptance.md`](06-answer-and-acceptance.md) | `appendix/examples/*/answers/`、`hidden-tests/` |
| 判定 | [`docs/03-grading.md`](03-grading.md) | `schemas/rubric|defect|feedback|grade.schema.json`、`appendix/personas/` |
| 隔离 | [`docs/04-isolation.md`](04-isolation.md) | `appendix/checklists/`、`reference/evalkit/canary.py` |

## 1. 仓库布局

```
llm-eval/
├── README.md                     # 导航 + 摘要 + 快速开始            [Lead]
├── docs/
│   ├── 00-conventions.md         # 本文
│   ├── 01-architecture.md        # 组件、数据流、部署形态            [Lead]
│   ├── 02-eval-flow.md           # trial 生命周期、状态机、预算、重判 [Lead]
│   ├── 03-grading.md             # L1/L2/L3、rubric、反馈阶梯、档位   [grading]
│   ├── 04-isolation.md           # 威胁模型、沙箱、金丝雀、作弊检测   [isolation]
│   ├── 05-task-library.md        # 题库分类、目录、出题流水线、防污染 [tasks-a]
│   ├── 06-answer-and-acceptance.md # 参考解、隐藏测试、验收与答案隐私 [tasks-a]
│   ├── 07-harness-mapping.md     # Harbor/Inspect 落地映射与适配层接口 [Lead]
│   └── 08-implementation-notes.md# 参考实现说明                    [impl]
├── schemas/                      # JSON Schema 2020-12
├── appendix/
│   ├── examples/<task-id>/       # 题目包（见第 3 节）
│   ├── personas/                 # 模拟用户 persona、F0-F3 话术模板  [grading]
│   ├── checklists/               # 隔离/卫生/上线检查清单           [isolation]
│   └── playbooks/                # 各平台执行、观察、验收 playbook   [tasks-b]
├── research/                     # 调研报告（全部带 URL 引用）        [research]
├── reference/                    # 可运行的 Python 参考实现（stdlib） [impl]
├── scripts/                      # 仓库自检脚本（链接/契约/Schema/测试） [Lead]
└── drafts/
    ├── plan-v1.md                # 用户原始方案（只读基线，不得修改）
    └── plan-v2.md                # 修订版主文档（Lead 在收尾时写）
```

## 2. 标识符

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 题目 ID | `<track>-<3位序号>`；track ∈ {logic, web, data, backend, debug, macos, ios, harmony, rust, vulkan, video, game} | `web-001` |
| 题目版本 | 语义化版本 `MAJOR.MINOR.PATCH`；MAJOR=验收语义变化需重跑，MINOR=验收变化需重判，PATCH=文案/资源修正可复用 | `1.2.0` |
| 模型 slug | 小写、去厂商前缀、连字符 | `claude-sonnet-5`、`gpt-5-codex`、`gemini-3-pro`、`kimi-k2.5` |
| 外壳 slug | `claude-code` / `codex-cli` / `opencode` / `kimi-code` / `openhands` / `terminus` | `claude-code` |
| trial ID | `{task_id}.{model}.{shell}.e{epoch}.t{index}`（epoch 从 1 起，trial index 从 1 起，两位补零） | `web-001.claude-sonnet-5.claude-code.e1.t02` |
| 批次 | `{YYYY-MM}-b{序号}` | `2026-11-b1` |
| 金丝雀令牌 | `CNRY-{trial_id 短哈希 8 位}-{随机 8 位}` | `CNRY-9f3a1c02-7d4e0b11` |
| 缺陷 ID | `{rubric_item_id}#{轮次}`，如 `R3#2` 表示第 2 轮发现的 R3 缺陷 | `R3#2` |

## 3. 题目包契约（`appendix/examples/<task-id>/`）

**必需文件**（缺一不可，CI 会检查；本文写作之时尚未接入 CI，由 Lead 人工核对）：

| 文件 | 内容要求 |
| --- | --- |
| `task.yaml` | 元数据，字段对齐 `schemas/task.schema.json`；必须含 id/version/track/platform/difficulty/hard_gates/rubric 引用/预算/资源策略/金丝雀位置 |
| `instruction.md` | 交给被测 Agent（或用户 Agent）的指令，真实用户口吻，不写实现提示 |
| `persona.md` | 模拟用户设定 + F0-F3 逐级可说话术 + 常见澄清问答（FAQ）+ 禁止泄露清单 |
| `rubric.yaml` | 原子化二元条目，字段对齐 `schemas/rubric.schema.json`，必含 `f1_symptom` |
| `defects.yaml` | 缺陷现象目录，字段对齐 `schemas/defect.schema.json`，供 F1/F2/F3 生成反馈 |
| `answers/reference-a.*` | 参考解 A（完整、可执行或可编译） |
| `answers/reference-b.*` | 参考解 B，**思路必须与 A 不同**（不同库/架构/算法） |
| `answers/empty-agent.md` | 空 Agent（无任何操作）的预期结果：必须判 Failed，并说明命中哪些硬门槛 |
| `hidden-tests/run.sh` | 隐藏验收入口（验证侧执行，不进 Agent 镜像），退出码 0=通过 |
| `hidden-tests/` 其余文件 | 测试代码、隐藏数据集、参考输出、哈希锁清单 |
| `canary.md` | 令牌写入位置清单（文件、注释、示例数据）与应出现的轨迹位置 |
| `notes.md` | 答案与验收说明：如何构建/运行/验证、已知容差、依赖版本、playbook 引用 |

**可选**：`evidence/`（预期截图说明用 markdown，禁止二进制）、`mutations.md`（变异测试清单）。

约定：
- 可本地执行的题（logic/data/debug/backend/video 的一部分）必须提供**能真跑**的 Python 代码与测试，用 `python3` 标准库，不依赖第三方包；运行验证用 `bash hidden-tests/run.sh`。
- 依赖真实工具链的题（macos/harmony/vulkan/ios）提供完整源码与精确命令；`TODO(run-on-host)` 以 `notes.md` 的汇总清单为准，每包**独立缺口 ≤3 个**（同一缺口在多份文件中引用不重复计），并写明验证环境。
- 隐藏测试与参考答案**不得**出现在 Agent 可见的镜像/工作区说明中；`task.yaml` 的 `resources` 只写声明，不写测试内容。

## 4. 跨文档共享的数据契约

以下枚举拼写是硬约定，任何文档/Schema/代码必须逐字一致。

### 4.1 Trial 状态（`trial.state`）

`pending` → `leased` → `running` → `observing` →（`awaiting_feedback` → `running`）* → `verifying` → `scored`；异常分支：`invalid`（基础设施/隔离问题作废）、`paused_manual`（人工介入）、`failed_infra`（重试后仍失败）。

- 合法转移表与失败处置写在 `docs/02-eval-flow.md`；Schema 只约束枚举。
- 重判（regrade）不新建 trial，而是产生新的 `grade` 版本，记录在 `trial.grades[]`。

### 4.2 反馈级别（`feedback.level`）

`F0`（无反馈）/ `F1`（现象描述）/ `F2`（日志、报错原文）/ `F3`（视觉描述、协助操作）。语义、升级规则、泄露检查见 `docs/03-grading.md`。

### 4.3 档位（`grade.tier`）

枚举按优劣排序：`P`（Perfect）> `A` > `B+` > `B` > `C+` > `C` > `D` > `F`（Failed）。
榜单展示：`Perfect / A / B+ / B / C+ / C / D / Failed`；聚合器用的数值映射：`P=7, A=6, B+=5, B=4, C+=3, C=2, D=1, F=0`。
档位判定规则（含半档条件、F0 特例、Failed 条件）见 `docs/03-grading.md`，本文只固定拼写与排序。

### 4.4 缺陷严重度与扣分权重

`minor`=1 分 / `functional`=3 分 / `fatal`=5 分。扣分按"每轮新发现缺陷计入一次"累计，未修复不重复扣。展示格式 `{扣分}/{档位}({费用,2位小数})`，如 `6/A(290.08)`。

### 4.5 检查类型（`check.kind`）

`script`（L1 硬门槛）/ `llm_judge`（L2 单条 rubric 判分）/ `agent_judge`（L2 能操作环境的裁判 Agent）/ `human`（L3）。

### 4.6 错误分类（`error.class`）

`provider` / `model` / `cheat` / `isolation` / `environment` / `verification` / `judge` / `version`。
处置（`error.disposition`）：`retry`（成绩无效重试）/ `score`（成绩有效照常判分）/ `zero`（判 0 并标记）/ `manual`（转人工）/ `discard`（作废重跑）。

### 4.7 成绩有效性（`trial.validity`）

`valid` / `invalid` / `pending` / `manual`。

### 4.8 预算字段（task.yaml \u2192 trial，字段名冻结）

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| \`max_rounds\` | 6 | 含首轮的总轮数上限（反馈轮数 = max_rounds-1） |
| \`max_feedback_rounds\` | 3 | 实际发出反馈的轮数上限（F0 无信息量催促不计入） |
| \`max_nudges\` | 2 | 无信息量催促次数上限，计 F0、不降档 |
| \`max_total_tokens\` | 2,000,000 | 被测 Agent 模型输入+输出总量 |
| \`max_wall_clock_s\` | 3600 | 从 running 开始计时的墙钟上限（秒） |
| \`max_cost_usd\` | 15 | 被测 Agent 侧费用上限（裁判/用户费用单列） |
| \`max_observe_s\` | 600 | 单轮观察阶段超时（秒） |
| \`escalation.f1_no_progress_rounds\` | 2 | 同一缺陷 F1 连续无进展多少轮升 F2 |
| \`escalation.f2_no_progress_rounds\` | 2 | 同一缺陷 F2 连续无进展多少轮升 F3 |

题目自定义值写入 \`task.yaml.budget\`，但不得超过批次硬上限；\`schemas/task.schema.json\` 必须包含以上字段。

## 5. Schema 清单

| 文件 | 维护者 | 内容 | 被谁引用 |
| --- | --- | --- | --- |
| `schemas/task.schema.json` | tasks-a | 题目元数据 | task.yaml、参考实现 loader |
| `schemas/rubric.schema.json` | grading | rubric 条目与集合 | task.yaml、03 |
| `schemas/defect.schema.json` | grading | 缺陷目录与观察结果 | 03、reference |
| `schemas/feedback.schema.json` | grading | 反馈决策与文本 | 03、reference |
| `schemas/grade.schema.json` | grading | 单次判分结果（含 tier/penalty/cost） | 02、03、trial |
| `schemas/trial.schema.json` | isolation | 完整 trial 记录（含隔离字段、轮次、成绩） | 02、04、reference |

- JSON Schema 用 draft 2020-12；`$id` 固定为 `https://llm-eval.local/schemas/<name>.schema.json`；跨文件引用用相对 `$ref`（如 `"grade.schema.json"`）。
- `description` 用中文，`properties` 名用英文 snake_case。
- 每个 schema 顶层必须 `additionalProperties: false`（可扩展字段统一放 `extensions` 对象）。

## 6. 文档格式

每篇文档开头：

```markdown
> 状态：v1 · 维护：<owner> · 最后更新：2026-11-06
> 上游：[00-conventions](00-conventions.md) · 相关：[02-eval-flow](02-eval-flow.md)
```

- 正文用简体中文；代码标识符、CLI 参数、字段名用英文。
- 外部事实、数字、工具能力必须给**可点击的 URL 引用**；调研文档尤其严格。
- 内部规格用相对链接互相引用，不重复抄写别的文档的规则，只给链接 + 关键枚举。
- 每篇规格文档尽量包含：目标 / 非目标 / 输入输出 / 关键流程或状态机 / 失败模式 / 验收标准。

## 7. 质量门槛（交付前自查）

1. 文档内所有相对链接指向的路径都存在（Lead 收尾时统一跑脚本检查）。
2. 示例题包必需文件齐全，`task.yaml` 与 `rubric.yaml` 字段不超出 Schema。
3. 可运行示例用 `python3` 实跑通过（提供 `run.sh` 的必须本地验一次）。
4. 调研文档的每条关键结论有 URL；不确定的写 `TODO(verify)`，全仓库此类标记不超过 20 处。
5. 不出现编造的基准数字：引用榜单数字必须注明来源与快照日期。

## 8. 协作规则

- 每位成员只写自己任务卡写明的写作用域；跨域修改先给 Lead 发消息。
- 发现别人产物与本文冲突：以本文为准先执行，同时消息 Lead，不要直接改他人文件。
- 完成任务后用 `send_message` 给 lead 发**简短**回执：改了哪些文件、关键结论 3-5 条、遗留 TODO。
