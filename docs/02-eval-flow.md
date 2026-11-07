# 02 · 评测流（Eval Flow）

> 状态：v1 · 维护：Lead · 最后更新：2026-11-06
> 上游：[00-conventions](00-conventions.md) · 相关：[01-architecture](01-architecture.md) · [03-grading](03-grading.md) · [04-isolation](04-isolation.md)

## 1. 目标与非目标

**目标**：把"一道题、一个模型、一次尝试"从排队到出分的全过程写成可执行、可测试、可重放的状态机，并固定超时、预算、重试、作废与重判的判定权归属。

**非目标**：不规定 Harbor 内部实现；不规定判分规则细节（见 [03-grading](03-grading.md)）；不规定沙箱实现（见 [04-isolation](04-isolation.md)）。

## 2. 名词与层级

| 层级 | 含义 | 标识 |
| --- | --- | --- |
| 批次 batch | 一次发榜的完整集合：模型列表 × 题库版本 × epoch 数 | `2026-11-b1` |
| 单元格 cell | 模型 × 题目（跨所有 epoch） | `web-001.claude-sonnet-5.claude-code` |
| trial | 单元格内的一次独立尝试（epoch × 重复序号） | `web-001.claude-sonnet-5.claude-code.e1.t02` |
| 轮次 round | trial 内的一次"Agent 干活 → 观察 → 反馈"循环 | `trial + n` |
| 阶段 phase | 轮次内或跨轮次的处理步骤（run/observe/feedback/verify/score） | 见第 4 节 |

- 一个 cell 默认 3 个 epoch，每个 epoch 可因重跑产生多个 trial 版本；**只有 `valid` 且最新一次的 trial 计入成绩**。
- Pass 继承与 Skip 是 cell 级人工状态，见第 10 节。

## 3. 批次级流程

```
Draft ──▶ Frozen ──▶ Running ──▶ Graded ──▶ Audited ──▶ Published ──▶ Archived
  │         │           │           │            │
  │         │           │           │            └─ 隔离体检 + 一致性报告通过才可发布
  │         │           │           └─ 所有 cell 出分或人工挂起
  │         │           └─ 资源就绪、预算锁定、版本矩阵不可变
  │         └─ 题目包 lint 通过、预算审批通过
  └─ 选题、选模型、定 epoch 与资源计划
```

| 阶段 | 进入条件 | 退出条件 | 失败处置 |
| --- | --- | --- | --- |
| Draft | 需求提出 | 计划表 + 预算表完成 | 退回 |
| Frozen | Linter 全绿；预算批准；镜像 digest 锁定 | 版本矩阵哈希写入 batch 记录 | 修正后重新冻结 |
| Running | 首批资源就绪 | 所有 cell 进入 已出分/Pass/Skip/paused_manual | 单 trial 失败不影响批次 |
| Graded | 所有 cell 有结果 | 聚合报告生成 | 人工队列清空 |
| Audited | 隔离体检 + 裁判一致性 + 抽查完成 | 审计报告签字 | 有问题则回 Running 重跑 |
| Published | 审计通过 | 榜单快照 + 报告发布 | — |
| Archived | 发布 90 天后 | 原始产物按保留策略清理，判分证据保留 | — |

## 4. Trial 生命周期状态机

### 4.1 状态

```
             ┌─────────┐  租约成功   ┌────────┐  预检通过  ┌─────────┐
             │ pending │───────────▶│ leased │───────────▶│ running │
             └────┬────┘            └───┬────┘            └────┬────┘
                  │                     │                      │ Agent 停止
                  │ 取消/计划作废        │ 预检失败/重试耗尽      ▼
                  │                     │                 ┌──────────┐
                  ▼                     ▼                 │ observing│
              ┌─────────┐         ┌──────────────┐        └────┬─────┘
              │ invalid │◀────────│ failed_infra │◀────────────┤
              └─────────┘ 重试耗尽 └──────────────┘   基础设施错误│
                  ▲                                            ▼
                  │                                 ┌──────────────────┐
                  │       达到终态条件               │ awaiting_feedback│
                  │       或全部硬门槛通过            └───────┬──────────┘
                  │                                            │ 反馈发出
                  │                                            ▼
                  │                                        ┌─────────┐
                  │                                        │ running │（下一轮）
                  │                                        └─────────┘
                  │
             ┌────┴─────┐  验证+判分完成   ┌────────────┐
             │ verifying│────────────────▶│  scored    │
             └────┬─────┘                 └──────┬─────┘
                  │ 验证不稳定/裁判不可用          │ 事后隔离审计命中
                  ▼                              ▼
            ┌──────────────┐               ┌─────────┐
            │ paused_manual│──────────────▶│ invalid │
            └──────────────┘   人工判定      └─────────┘
```

### 4.2 合法转移表

| 从 | 到 | 触发 | 守卫条件 | 副作用 |
| --- | --- | --- | --- | --- |
| pending | leased | 调度器派发 | 资源可用、版本冻结、预算未耗尽、cell 未 Skip | 创建 sandbox_lease/device_lease；写 `trial.leased` 事件 |
| pending | invalid | 批次取消 / 计划作废 | — | 记录原因；不重跑 |
| leased | running | 预检通过 | 镜像 digest 匹配、金丝雀注入成功、I1–I9 检查通过 | 启动外壳、开始计时与计费 |
| leased | failed_infra | 预检失败且重试 3 次仍失败 | — | 释放租约；按第 8 节重试 |
| running | observing | Agent 停止 | 停止原因已记录 | 冻结沙箱；启动观察 |
| running | failed_infra | 外壳/网关/SSH 基础设施异常 | 非模型原因，且重试策略允许 | 释放租约；按第 8 节重试 |
| running | paused_manual | 人工中断 | — | 保留沙箱 ≤30 min，超时转 invalid |
| observing | awaiting_feedback | 观察完成且未达终态 | 预算剩余、轮数未满、缺陷清单需要反馈 | 生成缺陷快照 |
| observing | verifying | 观察完成且达终态 | 硬门槛全过 / 预算耗尽 / 作弊命中 / 轮数用尽 / 不可继续 | 记录终态原因 |
| awaiting_feedback | running | 反馈已发送 | 泄露检查通过 + 未达预算 | 计一轮反馈；开启下一轮 |
| awaiting_feedback | verifying | 反馈无法生成（3 次泄露重写仍失败） | — | 记 `feedback_blocked`，按未通过判分 |
| verifying | scored | 隐藏测试 + 裁判完成，grade 已算 | 所有检查有结论 | 写 grade v1；计费结算 |
| verifying | paused_manual | 验证不稳定 / 裁判不可用 / 一致性低 | 重试与重判策略已穷尽 | 转人工队列 |
| verifying | failed_infra | 验证沙箱起不来 | 重试 3 次失败 | 释放租约；重跑 |
| paused_manual | running / observing / verifying | 人工恢复 | 人工给出决定 | 记录人工操作 |
| paused_manual | invalid | 人工判定作废 | — | 记录原因；重跑 |
| scored | invalid | 事后隔离审计命中 / 版本作废 | — | 触发重跑；保留记录 |
| failed_infra | pending | 重试预算未耗尽 | — | 重新排队（最多 3 次） |
| failed_infra | invalid | 重试耗尽 | — | 标记环境问题；不进排名 |

**终态**：`scored`、`invalid`。`paused_manual` 是准终态（可恢复）。`failed_infra` 是中间态（必须走向 pending 或 invalid）。

## 5. 阶段详解

### 5.1 计划与冻结（cell 级）

1. Batch Planner 从 `task.yaml` 读取平台、资源、预算，生成 cell 矩阵。
2. 冻结：题库版本、模型 slug + 官方供应商 + 版本号、外壳版本、镜像 digest、判分代码版本、rubric 版本、裁判 prompt 版本。生成 `versions.freeze_hash`。
3. 任何一项变化都必须开新批次；冻结后禁止"悄悄升级"。

### 5.2 租约与沙箱准备（pending → leased → running）

| 步骤 | 动作 | 失败处置 |
| --- | --- | --- |
| S1 | 按平台申请资源：Linux 容器 / macOS VM 槽位 / 鸿蒙设备 / GPU 节点 | 排队；超时 30 min 告警 |
| S2 | 从固定 digest 镜像创建沙箱（预热池实例必须从未跑过 trial） | 重试 2 次，换宿主 |
| S3 | 生成 trial 金丝雀令牌并埋点（见 [04-isolation](04-isolation.md)） | 失败即 invalid |
| S4 | 身份去标识：随机工作区路径、中性用户名、清空 `$HOME` 模板 | 失败即 invalid |
| S5 | 隔离预检 I1–I9（见 [04-isolation](04-isolation.md) 第 5 节） | 任一失败 → 换宿主重试 |
| S6 | 注入任务资源（非隐藏部分），启动外壳 | 失败按环境错误处理 |

> **不依赖 Harbor 默认值**：`environment_mode=separate`（老任务默认 shared）、`[agent] user`（非 root 是 opt-in）、`network_mode`、lint 的 `unpinned-image`/`root-agent` 规则（默认关闭，需 `--extend-select`）都必须在题包中显式声明并由预检断言。详见 [04-isolation](04-isolation.md) 与 [07-harness-mapping](07-harness-mapping.md)。

### 5.3 运行轮（running）

- 外壳在沙箱外运行，模型请求经 Gateway，工具调用经 SSH/ACP 单向进入沙箱。
- 外壳把 task instruction（或模拟用户初始消息）交给 Agent；被测 Agent 只知道当前轮次允许知道的信息。
- 心跳：外壳每 15 s 上报一次；3 次缺失即判定挂起。
- 停止原因枚举：`completed`（脚手架宣告完成）/ `idle`（无工具调用 N 分钟）/ `timeout`（墙钟）/ `cost_cap` / `token_cap` / `loop_detected`（同调用重复 N 次）/ `crash` / `cancelled` / `need_clarification`（Agent 提问）。

停止原因直接决定后续处置：

| 停止原因 | 含义 | 后续 |
| --- | --- | --- |
| completed | Agent 认为完成 | 进入 observe |
| idle | 长时间无动作 | 进入 observe；控制器可发"请按你的判断做完"（计 F0，限 2 次） |
| need_clarification | 合理澄清提问 | 模拟用户按 FAQ 回答；**不算反馈级别、不降档**；限 3 次 |
| timeout / cost_cap / token_cap | 预算耗尽 | 进入 observe → 直接 verifying（判 Failed 或按产物判） |
| loop_detected | 死循环 | 杀掉；进入 observe → verifying |
| crash | 外壳崩溃 | 先重试 1 次（同沙箱续跑或新沙箱重跑），仍失败按环境错误处理 |

### 5.4 观察（observing）

Observer 在 trial 沙箱内（只读模式）或经控制面执行：

1. **L1 可见验收**：编译/构建、启动、可见测试、产物存在与格式、数值容差、帧率/性能采集、截图、日志、无障碍树。输出 `observation[]` 与缺陷候选。
2. **缺陷归并**：与 `defects.yaml` 现象目录匹配，分配缺陷 ID（`R3#2`），更新跨轮状态（new/open/persisting/fixed）。
3. **终态判定**（按优先级）：
   - 作弊命中 → 终态，转 verifying（判 0）；
   - 硬门槛全过且不存在未修复的 functional/fatal 缺陷 → 终态，转 verifying（终判；只剩 minor 时最高 A，零缺陷才是 P）；
   - 轮数用尽 / 墙钟耗尽 / token 或费用耗尽 → 终态，转 verifying（判 Failed）；
   - 其余 → 转 awaiting_feedback。
4. 观察阶段总超时：Linux 10 min，macOS/GPU/设备侧 20 min；超时按"观察不完整"记缺陷并转下一轮（若预算允许）。

### 5.5 反馈决策与发送（awaiting_feedback）

1. Feedback Controller 读取缺陷跟踪 + 历史反馈 + 预算，按 [03-grading](03-grading.md) 的阶梯规则选择 `level`（F0–F3）与**一个**待反馈缺陷。
2. Simulated User 只拿到该级别白名单字段：F1 只给用户可见现象；F2 加日志/报错原文；F3 加视觉描述/协助操作。**永远看不到**参考答案、隐藏测试、rubric 分值。
3. 泄露检查：正则 + 小模型复核；命中禁止内容则重写，最多 3 次；3 次失败 → 转 verifying（记 `feedback_blocked`）。
4. 发送并记录：`feedback.sent`；trial 回到 running，轮次 +1。

### 5.6 验证（verifying）

1. **产物导出**：只拷 `task.yaml` 声明的产物路径；丢弃软链接/FIFO/setuid/设备文件；限制大小与数量；计算 sha256。
2. **验证沙箱**：独立新沙箱，仅放入产物 + 隐藏测试；清空旧 reward 文件；测试文件哈希锁校验。
3. **隐藏测试**：`hidden-tests/run.sh` 退出码 + 结构化结果；性能类测 5 次取中位数。
4. **裁判**：原子 rubric，多厂商评审团，每条采样 k=5 取中位数；分歧超阈值转人工（见 [03-grading](03-grading.md)）。
5. **grade 计算**：由 Grader 按确定性规则计算 tier/penalty/cost；写 `grade` v1。
6. **trial.scored**：状态落库，触发 teardown。

### 5.7 清理（teardown，异步但必须完成）

1. 杀进程组、卸载设备包、清理临时用户；归还设备/宿主。
2. 销毁沙箱（不可回收到预热池）。
3. 记录归还检查结果；异常写 `incident`。
4. 销毁后执行该 trial 的金丝雀扫描任务入队（批次结束后统一交叉扫描）。

## 6. 停止条件与优先级

终态判定按以下优先级短路，任何时刻只取第一条：

| 优先级 | 条件 | 终态 | 成绩 |
| --- | --- | --- | --- |
| P0 | 基础设施/隔离失效（金丝雀跨 trial、预检失败、宿主污染） | invalid | 无效，重跑 |
| P1 | 作弊命中（改测试、写 reward、硬编码、查答案、绕过网络、注入裁判） | verifying→scored | 有效，记 0 + 标记 |
| P2 | 硬门槛全过且无未修复 functional/fatal 缺陷 | verifying→scored | 正常判档（P/A） |
| P3 | 预算耗尽（轮数/墙钟/token/费用） | verifying→scored | Failed |
| P4 | 不可继续（反馈无法生成、Agent 连续崩溃但非环境问题） | verifying→scored | Failed |
| P5 | 人工中断 30 min 未恢复 | invalid | 无效，重跑 |

## 7. 预算模型

### 7.1 预算字段（trial 级，来自 task.yaml）

| 字段 | 默认值（标准题） | 重型题默认 | 说明 |
| --- | --- | --- | --- |
| `max_rounds` | 6 | 10 | 含首轮；反馈轮数 = max_rounds-1 |
| `max_feedback_rounds` | 3 | 5 | 实际发出反馈的轮数上限（F0 催促不计入） |
| `max_nudges` | 2 | 2 | 无信息量催促次数上限，计 F0、不降档 |
| `max_total_tokens` | 2,000,000 | 8,000,000 | 被测 Agent 的模型输入+输出总量 |
| `max_wall_clock_s` | 3600 | 10800 | 从 running 开始计时，不含排队与 teardown |
| `max_cost_usd` | 15 | 80 | 被测 Agent 侧费用上限（裁判/用户费用单列） |
| `max_observe_s` | 600 | 1200 | 单轮观察超时 |

字段名已冻结（见 [00-conventions](00-conventions.md) §4.8），`schemas/task.schema.json` 必须包含。`escalation.f1_no_progress_rounds` / `escalation.f2_no_progress_rounds` 默认 2 轮。

冷门平台题默认上浮 1.5 倍（macOS/鸿蒙/性能机）；题包可在 `budget` 中显式覆盖，但不得超过批次硬上限。

### 7.2 强制点

| 预算 | 强制点 | 超限行为 |
| --- | --- | --- |
| cost | Gateway 实时累计，每请求前检查 | 拒绝请求，停止原因 `cost_cap` |
| token | 外壳每步上报，Gateway 双校验 | 同上 |
| wall clock | 控制面计时器 | 杀进程组，停止原因 `timeout` |
| rounds | Controller 决策时检查 | 直接 verifying，终态 Failed |
| nudges | Controller 决策时检查 `max_nudges` | 不再发无信息量催促，转 F1 或终态 |
| 批次总预算 | Planner 冻结时锁定，Scheduler 每派发前检查 | 停止派发，告警 |

### 7.3 费用口径

```
trial.cost.agent = Σ_round (input_tokens × price_in + output_tokens × price_out + cache_read × price_cache)
trial.cost.judge = Σ_judge_call (裁判模型费用)
trial.cost.user  = Σ_feedback (模拟用户模型费用)
trial.cost.total = agent + judge + user
榜单单元格费用 = median(各 epoch 的 cost.agent)   # 与参考项目口径一致
批次成本 = Σ trial.cost.total + 基础设施成本（另表）
```

价格表按官方价锁定版本，写入 batch 记录；不得使用会变动的实时价。

## 8. 异常、重试、重判、作废

| 情形 | 分类 | 处置 | 成绩 | 重跑 |
| --- | --- | --- | --- | --- |
| 供应商限流/5xx/连接中断 | provider | 指数退避重试 ≤5 次；仍失败则 trial 转 pending 延后 | 无效 | 是 |
| 中转把请求路由到别的供应商/量化版 | provider | 响应头校验不一致即作废该 trial | 无效 | 是 |
| 安全拒答 | model | 记录拒答分类，照常判分 | 有效 | 否 |
| 上下文/输出超限、墙钟超时 | model | 按当时产物判分 | 有效 | 否 |
| 死循环/长时间无输出 | model | 杀掉，判预算耗尽 | 有效 | 否 |
| 声称完成但未完成 / 反复问"要不要继续" | model | 控制器发不含信息的"请按你的判断做完"，计 F0，限 2 次 | 有效 | 否 |
| 合理澄清提问 | model | 按 FAQ 回答，不算反馈级别 | 有效 | 否 |
| 搞坏环境（删工作区/改系统） | model | 不恢复，按产物判分 | 有效 | 否 |
| 留后台进程 | model | 导出前清进程组，验证在另一沙箱 | 有效 | 否 |
| 费用失控 | model | 达上限即停，判 Failed | 有效 | 否 |
| 改测试/硬编码/写 reward/查答案 | cheat | 判 0 + 标记 + 人工确认 | 有效（0） | 否 |
| 产物内含给裁判的指令 | cheat | 裁判 guard=penalize，判 0 + 标记 | 有效（0） | 否 |
| 访问白名单外网络/绕代理 | cheat（视情况） | 拦截 + 记录；绕过行为按作弊 | 视情况 | 视情况 |
| 金丝雀出现在别的 trial | isolation | 涉事 trial 全部作废，排查缺口 | 无效 | 是 |
| 镜像/沙箱起不来、磁盘满 | environment | 换宿主重试 ≤3 次 | 无效 | 是 |
| OOM | environment | 参考解同配额能跑通则算模型错，否则调配额重跑 | 视情况 | 视情况 |
| 设备掉线/VM 槽位泄漏 | environment | 摘除设备/宿主，换设备重试 | 无效 | 是 |
| 验证脚本超时/偶发失败 | verification | 用 regrade 重判 ≤3 次；仍不稳定则题目下线修复 | 无效 | 是 |
| 性能指标抖动 | verification | 5 次取中位数；测前检查温度与负载；阈值留余量 | 有效 | 否 |
| 裁判超时/输出无效 | judge | 重试；仍失败进人工队列，**不静默记 0** | 待定 | 否 |
| 裁判多次采样一致性低 | judge | 进人工队列 | 待定 | 否 |
| 外壳/SDK/浏览器升级导致漂移 | version | 镜像锁版本；升级视为新一期 | 有效 | 否 |
| 题目发现 bug | version | PATCH 复用旧 trial；MINOR 重判；MAJOR 重跑 | — | 视情况 |

**重试语义**：重试生成**新的 trial 记录**（新 trial ID 的 epoch 不变、trial index 递增），旧记录保持 `invalid`，不覆盖。**重判语义**：同一个 trial 新增加一条 `grade` 版本，不重新跑 Agent。

## 9. 调度与并发

### 9.1 调度算法（伪代码）

```python
while batch.active:
    for cell in ready_cells(batch):                  # 依赖：cell 未 Skip/Pass
        if cell.budget_exhausted: cell.skip("budget"); continue
        platform = cell.task_version.platform
        lease = inventory.try_reserve(platform, quota=cell.task_version.resources)
        if not lease:
            cell.wait(backoff=30s); continue          # 设备/Mac 槽位是硬瓶颈
        trial = create_trial(cell, lease)
        enqueue(trial)
    for host in inventory.heartbeat_stale(>60s):
        inventory.drain(host)                         # 摘除宿主，重派其 trial
    for lease in leases.expired():
        invalidate(lease.trial, reason="lease_timeout")
```

调度原则：
1. **资源不相交**：一个沙箱/设备/VM 槽位同一时刻只属于一个 trial。
2. **公平性**：同一模型的不同题目交错派发，避免某模型先跑完好题再跑坏题带来的时间偏置。
3. **优先级**：① 冷门平台（瓶颈资源，先排）；② 重跑（已延期 cell）；③ 常规 Linux 题；④ 参考解/空 Agent 对照（上线前阻塞项最高）。
4. **不抢占**：除基础设施故障外，不允许抢占运行中的 trial（避免半成品污染计时/帧率指标）。
5. **饥饿保护**：资源连续 2 h 未获取的 cell 升级告警并临时提高配额。

### 9.2 并发上限

| 平台 | 单位 | 并发决定因素 |
| --- | --- | --- |
| Linux 容器 | 每宿主 N 个沙箱 | CPU/内存/磁盘配额；建议每 8 核 4 个 |
| macOS Tart VM | 每台物理 Mac 2 个 VM | Apple 虚拟化许可 |
| 鸿蒙设备 | 每设备 1 个 trial | 物理设备数 |
| GPU 节点 | 每 GPU 1–2 个 | 显存与帧率测量稳定性 |
| 性能机 | 每机 1 个 | 测量互斥，必须独占 |

## 10. Cell 状态与榜单归属

| cell 状态 | 含义 | 是否进排名 |
| --- | --- | --- |
| Pending | 未开始 | 否 |
| Running | 至少一个 trial 在跑 | 否 |
| Scored | 所有 epoch 有有效成绩 | **是** |
| Pass | 人工标记"前代已 A，跳过" | 是（标注继承自哪个前代模型） |
| Skip | 人工标注原因跳过（不适用/题目退役） | 否（榜单显示"未测"） |
| Manual | 进入人工仲裁 | 暂不进 |

聚合规则：cell 得分 = 各 epoch 的**中位档位**；展示最好/最差档位；档位跨度 ≥2 档进人工复核。费用 = 中位 agent 费用。

## 11. 事件日志契约

事件信封（JSONL 一行）：

```json
{
  "event_id": "evt_01J...",
  "schema_version": 1,
  "ts": "2026-11-06T12:00:00Z",
  "batch_id": "2026-11-b1",
  "trial_id": "web-001.claude-sonnet-5.claude-code.e1.t02",
  "type": "feedback.sent",
  "actor": "controller",
  "payload": { "round": 2, "level": "F1", "defect_id": "R3#1", "leak_check": "pass" }
}
```

必需事件类型：

`trial.created`、`trial.leased`、`sandbox.provisioned`、`canary.injected`、`preflight.result`、`trial.started`、`round.started`、`agent.step`、`agent.stopped`、`observe.started`、`hardgate.result`、`evidence.collected`、`defect.updated`、`feedback.decided`、`feedback.leakcheck`、`feedback.sent`、`budget.threshold`、`verify.started`、`hiddentest.result`、`judge.verdict`、`grade.computed`、`trial.scored`、`trial.invalidated`、`teardown.started`、`teardown.completed`、`canary.scanned`、`incident.opened`。

事件必须**只追加**；状态由事件流投影得到，Postgres 只是投影缓存。任何状态变更若无对应事件即为 bug。

## 12. 典型时序

**A. 一次过（F0，Perfect）**：pending→leased→running（Agent 完成）→observing（硬门槛全过、零缺陷）→verifying→scored（P/0 分/$3.2）。

**B. 一轮 F1 修复（B）**：observing 发现 2 个 functional 缺陷→awaiting_feedback（F1，只讲主缺陷）→running→observing（修复 1 个）→awaiting_feedback（F1）→running→observing（全过）→verifying→scored（B：need_feedback=2、total=2，不满足 3×need_feedback ≤ total，无半档）。若同题另有 4 个 minor 缺陷只观察不反馈，则 need_feedback=2、total=6 满足 3×2≤6，升半档 B+。

**C. 升级到 F2（D）**：同一缺陷 F1 连续 2 轮无进展→F2 给日志→修复→全过→scored（D）。

**D. 预算耗尽（Failed）**：8 轮后仍未过硬门槛→observing 判终态→verifying（隐藏测试失败）→scored（F，扣分按已发现缺陷累计）。

**E. 作弊（0 分 + 标记）**：Observer 发现测试文件哈希变化→cheat 命中→verifying→scored（F，penalty=0，cheat_flag=true）→人工确认→公示。

**F. 隔离失效（作废重跑）**：批次后金丝雀扫描发现 trial A 产物含 trial B 令牌→A、B 及同宿主 trial 标 invalid→incident→排查缺口→重跑。

## 13. 验收标准（可用测试用例表达）

> 档位与停止条件的 12 个边界用例以 [03-grading](03-grading.md) 的判定表为准，参考实现需逐条转成测试。

| # | 场景 | 期望 |
| --- | --- | --- |
| T01 | 正常一轮通过 | trial 走完 6 个状态，事件流完整，grade 有版本号 |
| T02 | 硬门槛失败 → F1 → 修复 | 轮次、反馈级别、档位与 [03-grading](03-grading.md) 规则一致 |
| T03 | 同一缺陷 F1×2 无进展 | 第 3 次反馈升级 F2；档位 D |
| T04 | 预算耗尽 | 停止原因 `cost_cap`，trial 有效，档位 Failed |
| T05 | 网关 5xx ×3 | trial invalid + 新 trial 重跑；旧记录保留 |
| T06 | 金丝雀跨 trial | 两个 trial 均 invalid；incident 创建 |
| T07 | 改测试文件 | cheat_flag=true，penalty=0，不进人工前不公示 |
| T08 | 裁判 API 超时 | 进人工队列（validity=manual），不静默记 0 |
| T09 | 重判 | 同一 trial 产生 grade v2，trial 状态仍 scored |
| T10 | 设备归还失败 | incident + 该设备摘除，后续 trial 不派发 |
| T11 | 预热池复用 | 跑过 trial 的实例不得回池（调度器拒绝） |
| T12 | 事件重放 | 从事件流重建 trial 状态 = 数据库状态 |

## 14. 与其他文档的接口

- 判定规则、档位、扣分、裁判标定 → [03-grading](03-grading.md)
- 隔离不变量、金丝雀、验证沙箱、设备池 → [04-isolation](04-isolation.md)
- 题目包字段、预算默认值、验收脚本 → [05-task-library](05-task-library.md) / [06-answer-and-acceptance](06-answer-and-acceptance.md)
- Harbor 落地映射与适配层接口 → [07-harness-mapping](07-harness-mapping.md)
- 可运行参考实现 → [reference/README](../reference/README.md)、[08-implementation-notes](08-implementation-notes.md)
