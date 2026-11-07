# 05 · 题库（Task Library）

> 状态：v1 · 维护：tasks-a · 最后更新：2026-11-06
> 上游：[00-conventions](00-conventions.md) · 相关：[01-architecture](01-architecture.md) · [03-grading](03-grading.md) · [04-isolation](04-isolation.md) · [06-answer-and-acceptance](06-answer-and-acceptance.md) · Schema：[task.schema.json](../schemas/task.schema.json)

## 1. 目标与非目标

**目标**：把「题目」变成可审计的资产：分类法与 30 题目录、元数据契约（task.yaml）、资源/网络/预算默认值、出题与上线流水线、防污染手段、难度标定与生命周期。所有数字都是**本方案的默认值**，随标定回填；引用外部事实时给 URL。

**非目标**：不写判分规则（见 [03-grading](03-grading.md)）；不写隐藏测试设计细节（见 [06-answer-and-acceptance](06-answer-and-acceptance.md)）；不写沙箱实现（见 [04-isolation](04-isolation.md)）；不承诺外部基准的具体分数。

## 2. 三层分类法与 12 track

三层回答「这道题在测什么能力」，track 回答「用什么工具链与观察手段」。

| 层 | 定义 | 典型能力 | 主要判据 |
| --- | --- | --- | --- |
| logic 逻辑 | 无真实工程产物，答案是离散对象或短文本 | 归纳、约束搜索、空间推理、概率、长文抽取、抗诱导 | 答案比对 + 推导覆盖度（脚本 + 裁判） |
| engineering 常见工程 | 主流工具链的工程交付 | 前端、数据、后端、调试、Rust 桌面、视频生成 | 隐藏测试/API 测试/渲染检查 + 原子 rubric |
| platform 冷门平台 | 依赖特定 SDK/设备/GPU/真机的题 | 查文档、试错、平台惯用法 | 构建/设备/帧率/验证层等硬门槛 + 平台 rubric |

| # | track | 层 | 练什么 | 典型硬门槛 | 观察手段 | 平台 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | logic | logic | 规则归纳、约束、空间、概率、抽取 | 答案比对（离散唯一解） | 答案文件 + 推导文本 | linux-container |
| 2 | web | engineering | 前端交互、可视化、动画、无障碍 | Playwright 交互测试、截图比对 | 无头浏览器截图、DOM/控制台 | linux-container |
| 3 | data | engineering | 清洗、聚合、口径、容差 | 隐藏数据集结果一致（容差） | 报告 JSON + 脚本源码 | linux-container |
| 4 | backend | engineering | API、鉴权、限流、幂等、降级 | API 测试、压测、故障注入 | 日志、指标、trace | linux-container（compose 依赖） |
| 5 | debug | engineering | 无提示修复、回归防护 | 隐藏回归测试、测试哈希锁 | 测试输出、diff | linux-container |
| 6 | rust | engineering | 桌面/CLI 工程、异步 | cargo test、窗口/CLI 检查 | Xvfb 截图、stdout | linux-container-xvfb |
| 7 | video | engineering | 代码生成动画、编码参数 | ffprobe 时长/分辨率/编码 | 抽帧、音画同步 | linux-container |
| 8 | macos | platform | Metal/SwiftUI、性能优化 | 裸机帧时间达标（VM 只做功能） | 帧时间日志、Instruments 导出 | tart-macos + bare-metal-perf |
| 9 | ios | platform | SwiftUI + 服务端协同 | XCUITest + API 测试 | 模拟器截图、接口日志 | tart-macos（Xcode + simulator） |
| 10 | harmony | platform | ArkTS + NAPI、设备运行 | HAP 构建签名 + 设备功能测试 | hdc 截图/日志 | linux-container 编译 + harmony-device-pool |
| 11 | vulkan | platform | 图形管线、同步、验证层 | 验证层零报错 + 参考图相似度 | 截图、验证层日志 | gpu-node |
| 12 | game | platform | 引擎工作流、脚本化验收 | 回放状态比对、帧率下限 | 截帧、状态快照 | linux-container |

**分类判定规则**：一道题只能归入一个 track（由主产物决定）；层由 track 决定但允许特例——若产物是可执行代码且答案需要脚本比对，归 engineering。跨 track 的题必须拆成两道，不允许一道题两个 track。

## 3. 30 题目录

ID 规则见 [00-conventions](00-conventions.md) §2。星号 = 本仓库已带完整示例题包（appendix/examples/）。预算列格式：token（万）/墙钟（s）/费用（USD）；详细默认值见第 6 节。

| # | ID | 名称 | track | 层 | 平台 | 难度 | 硬门槛类型 | rubric 重点 | 预算 | 目标档 | 版本 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | logic-001* | 无解说对局记录归纳规则并预测下一步 | logic | logic | linux-container | 3 | 答案比对（唯一解） | 推导证据、唯一性说明 | 40/1800/3 | A-B | 1.0.0 | active |
| 2 | logic-002* | 折叠序列与孔洞位置 | logic | logic | linux-container | 3 | 答案比对（唯一解） | 折叠过程、洞来源 | 30/1800/2.5 | A-B | 1.0.0 | active |
| 3 | logic-003 | 带隐藏约束的排班/装箱 | logic | logic | linux-container | 4 | 唯一解 + 可行性校验 | 约束传播、最优性证明 | 40/1800/3 | B-C+ | 0.9.0 | draft |
| 4 | logic-004 | 20 问式多轮试探（预算内定位隐藏数） | logic | logic | linux-container | 4 | 交互步数 + 答案 | 提问策略、信息量 | 60/2400/4 | B | 0.9.0 | draft |
| 5 | logic-005 | 长文表格抽取（多页、口径冲突） | logic | logic | linux-container | 2 | 字段级集合比对 | 抽取完整性、口径处理 | 30/1500/2 | P-A | 0.9.0 | draft |
| 6 | logic-006 | 反诱导：题面含错误前提，须指出并给正确结论 | logic | logic | linux-container | 3 | 答案 + 标注错误前提 | 抗诱导、依据引用 | 30/1500/2 | A-B+ | 0.9.0 | draft |
| 7 | web-001* | 看板拖拽排序 + 暗色模式 | web | engineering | linux-container | 3 | Playwright 交互测试 | 拖拽/持久化/键盘/空状态/对比度 | 150/3600/8 | A-B | 1.0.0 | active |
| 8 | web-002 | 电磁感应教学页（参数联动） | web | engineering | linux-container | 4 | 数值与解析解误差 | 科学正确性、教学可用性 | 150/3600/8 | A-B | 0.9.0 | draft |
| 9 | web-003 | 从零写 WebGL 数据可视化（无第三方库） | web | engineering | linux-container | 4 | 截图相似度 + 交互 | 视觉还原、渲染正确性 | 150/3600/8 | B | 0.9.0 | draft |
| 10 | web-004 | 时间轴动画编辑器（逐帧验收） | web | engineering | linux-container | 4 | 固定时间点抽帧比对 | 动效、状态机、可访问性 | 150/3600/8 | B | 0.9.0 | draft |
| 11 | web-005 | 无障碍改造（键盘/读屏语义） | web | engineering | linux-container | 3 | axe 规则 + 交互测试 | a11y 语义、焦点管理 | 100/3000/6 | A-B+ | 0.9.0 | draft |
| 12 | data-001* | 脏日志清洗与固定口径报告 | data | engineering | linux-container | 2 | 隐藏数据容差比对 | 口径正确、抗硬编码、可维护 | 50/2400/4 | P-A | 1.0.0 | active |
| 13 | data-002 | 多源 CSV 合并去重（模糊匹配） | data | engineering | linux-container | 3 | 隐藏数据容差比对 | 匹配策略、规则说明 | 50/2400/4 | A-B | 0.9.0 | draft |
| 14 | data-003 | 流式窗口统计（内存上限） | data | engineering | linux-container | 4 | 结果 + 峰值内存 | 算法效率、正确性 | 60/3000/5 | B | 0.9.0 | draft |
| 15 | backend-001 | 带鉴权/限流/幂等的订单服务 | backend | engineering | linux-container | 4 | API 测试 + 压测 + 故障注入 | 错误处理、可观测性 | 150/5400/10 | A-B | 1.0.0 | active |
| 16 | backend-002 | 限流与熔断（依赖故障注入） | backend | engineering | linux-container | 4 | 故障注入后行为 | 降级策略、恢复 | 100/4200/8 | B | 0.9.0 | draft |
| 17 | backend-003 | 事件溯源账本（重放一致） | backend | engineering | linux-container | 5 | 重放/并发一致性测试 | 模型设计、版本迁移 | 150/5400/10 | B-C+ | 0.9.0 | draft |
| 18 | debug-001 | 台账工具无提示修复（5 处缺陷） | debug | engineering | linux-container | 3 | 隐藏回归 + 测试哈希锁 | 最小改动、根因修复 | 100/3600/6 | A-B | 1.0.0 | active |
| 19 | debug-002 | 并发竞态修复（可复现） | debug | engineering | linux-container | 4 | 压力回归测试 | 复现能力、同步正确性 | 100/4200/7 | B | 0.9.0 | draft |
| 20 | debug-003 | 内存泄漏定位与修复 | debug | engineering | linux-container | 4 | 内存曲线 + 回归 | 测量方法、修复范围 | 100/4200/7 | B | 0.9.0 | draft |
| 21 | rust-001 | 跨平台 Markdown 编辑器（实时预览） | rust | engineering | linux-container-xvfb | 4 | cargo test + 窗口截图 | 交互细节、状态管理 | 150/5400/10 | B | 0.9.0 | draft |
| 22 | rust-002 | 异步数据管道 CLI | rust | engineering | linux-container | 3 | cargo test + 端到端 | 错误处理、背压 | 100/3600/6 | A-B | 0.9.0 | draft |
| 23 | video-001 | 30 秒片头动画导出 MP4 | video | engineering | linux-container | 3 | ffprobe 时长/分辨率/编码 | 节奏、观感、音画同步 | 100/4200/8 | A-B | 1.0.0 | active |
| 24 | macos-001 | Metal 粒子渲染 20→60fps | macos | platform | tart-macos + bare-metal-perf | 5 | 裸机帧时间中位数达标 | 优化方法、GPU 并行 | 250/7200/20 | B-C+ | 1.0.0 | active |
| 25 | macos-002 | SwiftUI 菜单栏工具（沙箱权限） | macos | platform | tart-macos | 3 | XCTest + 截图 | 平台惯用法、权限处理 | 150/5400/12 | A-B | 0.9.0 | draft |
| 26 | ios-001 | SwiftUI 客户端 + Rust 服务离线同步 | ios | platform | tart-macos + 容器 | 5 | XCUITest + API 测试 | 状态管理、冲突处理 | 250/7200/20 | B | 0.9.0 | draft |
| 27 | harmony-001 | ArkTS 页面 + NAPI C++ 图像处理 | harmony | platform | linux-container + harmony-device-pool | 5 | HAP 构建签名 + 设备功能 | ArkTS/NAPI 惯用法 | 250/7200/20 | B-C+ | 1.0.0 | active |
| 28 | harmony-002 | 分布式数据跨设备同步 | harmony | platform | harmony-device-pool | 5 | 双设备行为测试 | 设备管理、异常恢复 | 250/7200/20 | C+ | 0.9.0 | draft |
| 29 | vulkan-001 | 从零渲染管线（阴影场景） | vulkan | platform | gpu-node | 5 | 验证层零报错 + 截图相似度 | 资源同步、管线正确性 | 250/7200/25 | B-C+ | 1.0.0 | active |
| 30 | game-001 | Godot 平台跳跃（回放验收） | game | platform | linux-container | 4 | 输入回放状态比对 | 关卡设计、状态可复现 | 150/5400/10 | B | 0.9.0 | draft |

**覆盖比例与配额建议**

| 层 | 题数 | 当前占比 | 建议目标 | 说明 |
| --- | --- | --- | --- | --- |
| logic | 6 | 20% | 20% | 便宜、可参数化、适合标定裁判 |
| engineering | 17 | 57% | 50% | 主力：可自动化、可规模化 |
| platform | 7 | 23% | 30% | 差异化，但并发与成本受限（macOS 每机 2 VM） |

track 配额：logic 6、web 5、data 3、backend 3、debug 3、rust 2、video 1、macos 2、ios 1、harmony 2、vulkan 1、game 1。**禁止超过 30% 的题集中在同一 track**；同一 track 内至少 1 道难答题（难度 4-5）与 1 道标定用的简单题（难度 1-2）。

**预算分档（与第 6 节一致）**：S = 40 万 token/1800s/3 USD（logic 为主）；M = 100-150 万/3600-5400s/6-10 USD（engineering）；L = 250 万/7200s/20-25 USD（platform）。表内单位为「万 token/秒/USD」。

## 4. 题目元数据设计（对齐 task.schema.json）

一个题目 = 一个目录 [appendix/examples/](../appendix/examples/)，必需文件见 [00-conventions](00-conventions.md) §3；元数据写在 [task.yaml](../appendix/examples/logic-001/task.yaml)，由 [task.schema.json](../schemas/task.schema.json) 校验（draft 2020-12，顶层 additionalProperties:false，未定义字段放 extensions）。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| id / version | 是 | ID 规则与语义化版本（00-conventions §2） |
| track / layer | track 必填 | 12 track 之一；layer 属于 logic/engineering/platform |
| title / subtitle | title 必填 | 中文短标题 + 一句补充 |
| platform / difficulty | 是 | 平台规范名 + 难度 1-5（标定后回填；difficulty_calibration 记录状态） |
| instruction / persona | 是 | 固定指向 instruction.md / persona.md |
| rubric_ref / defects_ref | rubric 必填 | 指向 rubric.yaml / defects.yaml（$ref [rubric.schema.json](../schemas/rubric.schema.json)） |
| resources | 是 | 镜像/CPU/内存/磁盘/OS/工具；只声明，不写测试内容 |
| isolation | 是 | agent_user / environment_mode / network_mode 显式声明，不依赖框架默认值；可加 lint_extend_select（unpinned-image、root-agent） |
| network / tools | 否 | 题级网络策略与工具白名单（推荐填写，便于审计） |
| budget | 是 | 见第 6 节与 00-conventions §4.8 |
| hard_gates | 是 | L1 脚本硬门槛，全部 required，任一失败直接 Failed |
| stop_conditions | 否（推荐） | 成功条件：硬门槛全过且无未修复 functional/fatal；minor 只影响 P/A；预算耗尽 → Failed + budget_exhausted 标记 |
| reference_solutions | 二选一必填 | 至少 2 份、思路标签不同、路径指向 answers/reference-* |
| artifacts | 否（推荐） | 声明进入验证侧的产物与大小上限；隐藏测试/答案不得声明为产物 |
| canary | 是 | 埋点清单（canary.md）与预期轨迹 |
| verification | 否 | 验证入口、超时、变异脚本 |
| status | 是 | draft / review / active / deprecated / retired |

**isolation 必填的理由**：Harbor 的「Agent 非 root、独立环境、断网」都是 opt-in 而非默认（见 [Harbor 文档索引](https://docs.harborframework.com/llms.txt) 与 [04-isolation](04-isolation.md) 的核实记录）。评测题一律 agent_user=agent、environment_mode=separate、network_mode=no-network（个别冷门平台题经批准用 allowlist）。lint_extend_select 至少含 unpinned-image 与 root-agent，让缺失 digest 或以 root 运行的镜像在 lint 阶段直接失败。

**示例（logic-001 的片段）**

```yaml
isolation:
  agent_user: agent
  environment_mode: separate
  network_mode: no-network
  lint_extend_select: [unpinned-image, root-agent]
budget:
  max_rounds: 6
  max_feedback_rounds: 3
  max_nudges: 2
  max_total_tokens: 400000
  max_wall_clock_s: 1800
  max_cost_usd: 3
  max_observe_s: 300
  currency: USD
  escalation: {f1_no_progress_rounds: 2, f2_no_progress_rounds: 2}
```
## 5. 资源策略

**网络三模式**（与 isolation.network_mode 一一对应，映射到代理配置）：

| network_mode | 用途 | 代理规则 | 允许的题 |
| --- | --- | --- | --- |
| no-network | 默认 | 完全断网；搜索 MCP 关闭 | 绝大多数题（logic/web/data/backend/debug/rust/video/game） |
| allowlist | 冷门平台题查官方文档 | 只放行 docs_whitelist 中的域名前缀；搜索代理带黑名单（自家仓库/榜单/题库域名） | harmony、macos、ios、vulkan 文档类题 |
| public | 特例 | 全联网 | 需 Lead 批准；每题单独记录理由与 URL 审计要求 |

**文档白名单**（allowlist 模式）：只写官方文档站，例如华为 HarmonyOS 开发者文档、Apple Developer Documentation、Khronos Vulkan 规范；不得放行 GitHub 搜索、模型厂商 blog、任何榜单站点。白名单变更按题目 MINOR 版本处理（影响可比性）。

**工具矩阵**：Agent 侧统一 MCP 注入（给 Agent 一样的「眼睛」）：bash、read/write/edit、browser、screenshot、web_search（仅 allowlist 题）、gpu_profile（GPU 节点）、device_control（设备池）、ffmpeg（视频题）。工具集合写入 task.yaml.tools，验收侧只按声明提供，超出即 hard gate 失败并记 error.class=environment。

**镜像**：按 digest 固定（image_digest 禁止 TODO 上线）；依赖预装进镜像，禁止运行时联网安装；包管理器缓存只读。**设备**：macOS VM 每台物理机最多 2 个（[Apple 开发者论坛讨论](https://developer.apple.com/forums/activities/1586080)），鸿蒙真机按 trial 独占租用；GPU 节点每 trial 独立容器并限额。

## 6. 预算默认值（字段名以 00-conventions §4.8 为准）

| 档位 | 适用 | max_rounds | max_feedback_rounds | max_nudges | max_total_tokens | max_wall_clock_s | max_cost_usd | max_observe_s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S | logic | 6 | 3 | 2 | 400,000 | 1,800 | 3 | 300 |
| M | data/debug/rust/backend | 6-8 | 3-4 | 2 | 1,000,000-1,500,000 | 3,600-5,400 | 6-10 | 600 |
| L | macos/ios/harmony/vulkan | 8 | 4 | 2 | 2,500,000 | 7,200 | 20-25 | 900 |

- 所有题必须写全 8 个预算字段 + escalation（2/2）；题级值不得超过批次硬上限。
- **币种口径**：max_cost_usd 单位固定为 USD（模型官方 API 价；字段名不改）。task.yaml 可写 currency: USD（默认）与 pricing_table_version（价格表版本，如 2026-11-a）。与参考项目（数据单位为人民币）对照时只在**报表层**按固定汇率折算 CNY，不进入题包预算与 grade 的费用字段（grade 记录自带 currency，见 [03-grading](03-grading.md)）。
- 预算判定：达到任一上限即停，判 Failed 并带 budget_exhausted 标记；该 trial 成绩有效（模型自身失败），不重试。
- 裁判与模拟用户费用**单列**，不计入被测 Agent 的 max_cost_usd。

## 7. 出题流水线

状态机（每一步有明确产出与退出条件；任一步不通过不得进入下一步）：

```text
草稿 → 指令 → 参考解×2 → 隐藏测试 → 变异测试 → 空 Agent 对照 → 难度标定 → 评审 → 上线 → 滚动退役
 B1     B2       B3          B4           B5          B6              B7         B8    B9      B10
```

| 步骤 | 输入 | 产出 | 退出条件（DoD） |
| --- | --- | --- | --- |
| B1 草稿 | 能力缺口/榜单分析 | 题目草案：层/track/平台/目标档位/预算档 | 评审会通过选题（不在 B1 写代码） |
| B2 指令 | 草案 | instruction.md + persona.md + task.yaml 骨架 | 真实用户口吻；无实现提示；FAQ 覆盖 80% 合理提问 |
| B3 参考解×2 | 指令 | answers/reference-a.* / reference-b.* | 两种思路、不共享代码；均可运行/可编译（或标注 ≤3 处 TODO(run-on-host)） |
| B4 隐藏测试 | 参考解 | hidden-tests/ + run.sh + hashes.json | 参考解全过、空 Agent 全失败、与可见样例分离 |
| B5 变异测试 | 隐藏测试 | mutations.md + 变异脚本 | 至少 5 个变异体被杀（见 [06](06-answer-and-acceptance.md) 第 5 节） |
| B6 空 Agent 对照 | 题包 | answers/empty-agent.md + 记录 | 必须判 Failed 且写明命中哪些硬门槛 |
| B7 难度标定 | 题包 | difficulty_calibration | 2-3 个前代模型实测，最强模型落 A-B（简单题 P-A），否则调难度或退役 |
| B8 评审 | 全量文件 | 评审记录（checklist 第 10 节） | checklist 全过；隔离声明与预算齐全 |
| B9 上线 | 评审记录 | status=active + 版本 | 批次冻结时纳入版本矩阵；rubric/hidden 哈希入库 |
| B10 退役 | 上线数据 | status=deprecated/retired | 退役题公开策略与相似度剔除完成（第 8 节） |

**参数化题的额外要求**：生成器 + 求解器 + 唯一性校验（logic 类），生成器与隐藏测试同属验证侧，不得出现在 Agent 工作区；每个 trial 可用 task.yaml.extensions.generation.seed 派生实例，但要保证同一 trial 可重放（seed 记入 trial 记录）。

## 8. 防污染（题目被背下来）

| 手段 | 做法 | 验收断言 |
| --- | --- | --- |
| 私有化改编 | 题目基于真实工程改造，不复用公开 benchmark 原题；素材来源留档 | 与公开题相似度检查通过；题目不出现在公开仓库 |
| 参数化生成器 + 求解器 | 每次 trial 从种子生成实例，求解器验证唯一解；生成器只在验证侧 | 生成器哈希锁；generator 自检通过；同一 trial 可重放 |
| API 版本新鲜度 | 冷门平台固定 SDK 版本，优先选该版本新增/变更 API，区分「真会」与「背过旧答案」 | task.yaml 记录 SDK 版本；镜像 digest 固定 |
| 相似度剔除 | 对候选题目与现役题做 n-gram/winnowing 指纹比对，相似度高则拆题或退役旧题 | 相似度报告随题包归档 |
| 退役题公开策略 | 退役题可公开题面，但参考答案/隐藏测试永不公开；公开后不得再加入现役题 | 退役清单与公开记录 |
| 榜单站点屏蔽 | 搜索代理黑名单屏蔽自家仓库、榜单域名、题库站点；审计抓取 URL | 网络日志无黑名单命中 |
| 参考答案隔离 | 参考解与隐藏测试只在验证侧，Agent 镜像/工作区不可见 | 工作区哈希清单；harbor lint 无测试泄进镜像 |
| 答案泄露检测 | 对模型输出做与隐藏答案的相似度/逐字命中检查；对训练语料污染用 Min-K% 类检测思路做抽检 | 泄露事件为 0；命中即作废 |

外部依据（区分「文档明说」与「本方案推断」）：
- LiveCodeBench 用**按时间滚动更新的题目**来避免污染，属于「文档明说」的污染控制实践（[arXiv:2403.07974](https://arxiv.org/abs/2403.07974)）；本方案借其思路做按月滚动与相似度剔除，属推断。
- SWE-bench+ 的测量显示 SWE-bench 中 **32.67% 的成功补丁属于 solution leakage（答案直接出现在 issue/评论里）、31.08% 属于测试过弱**（[arXiv:2410.06992](https://arxiv.org/abs/2410.06992)）；这直接支撑「隐藏数据 + 隐藏测试 + 泄漏扫描」三项要求。
- EvalPlus 表明基础测试集不足会高估正确率，需**额外测试**（[arXiv:2305.01210](https://arxiv.org/abs/2305.01210)）；本方案据此要求隐藏测试规模显著大于可见样例。
- Min-K% Prob 提出用低概率词检测训练数据是否包含某样本（[arXiv:2310.16789](https://arxiv.org/abs/2310.16789)）；本方案仅在**抽检**中参考其思路，不作为判定依据（推断边界）。
- Harbor 提供独立验证沙箱、受限网络、奖励作弊检测与 regrade（[文档索引](https://docs.harborframework.com/llms.txt)、[防奖励作弊公告](https://docs.harborframework.com/news/reward-hacking.md)）。
## 9. 难度标定与上线闸门

**标定协议**：新题先用 2-3 个前代模型（不同厂商）各跑 3 个 trial；报中位档位；目标是最强模型落 A-B（简单题 P-A），且 3 次结果跨两档以上的题进人工复核。**样本量**：每题至少 9 个 trial（3 模型 × 3 次）才允许回填 difficulty_calibration.status=calibrated。

**上线闸门（全过才可 status=active）**

| 闸门 | 标准 |
| --- | --- |
| G1 结构 | check_repo 无 error；必需文件齐全；YAML 可解析且字段不超 Schema |
| G2 可运行 | 可本地执行的题：hidden-tests/run.sh 本地实跑通过（logic/data/debug/video/部分 backend） |
| G3 参考解 | 两份参考解都通过隐藏验收；两份思路不同且不共享代码 |
| G4 空 Agent | 必须 Failed，并在 empty-agent.md 写明命中门槛 |
| G5 变异 | 至少 5 个变异体被杀；变异清单归档 |
| G6 答案唯一性 | logic 类必须有求解器唯一性断言；data 类有容差依据 |
| G7 隔离 | isolation 三字段必填；金丝雀 5 处埋点；lint 扩展启用 |
| G8 难度 | 标定完成，最强模型落目标区间；否则调难度 |
| G9 评审 | 评审 checklist 全过；rubric 与 defects 一一对应 |

## 10. 题目评审 checklist（每题上线前逐条勾选）

1. [ ] instruction 是真实用户口吻，无实现提示、无隐藏测试痕迹
2. [ ] persona 有 F0-F3 话术、FAQ、禁止泄露清单
3. [ ] task.yaml 通过 task.schema.json；isolation 三字段与 lint_extend_select 齐全
4. [ ] budget 8 字段 + escalation 齐全，币种 USD，pricing_table_version 已填
5. [ ] hard_gates 全部 required，pass_condition 可执行、无歧义
6. [ ] rubric 8-15 条原子条目，无合取问句，f1_symptom 不含文件名/API/日志原文
7. [ ] defects 覆盖全部 functional/fatal rubric 条目（F1/F2/F3 素材）
8. [ ] 两份参考解思路不同、独立实现、本地跑通或 ≤3 处 TODO(run-on-host)
9. [ ] 空 Agent 对照必须 Failed，命中门槛写清
10. [ ] 隐藏测试与可见样例分离；测试文件哈希锁
11. [ ] 至少 5 个变异体被杀，mutations.md 归档
12. [ ] 答案比对脚本输出机器码，不回传判定口径（logic 类）
13. [ ] 容差有依据（data 类）；性能测量有中位数与预算（macos/vulkan/video）
14. [ ] 金丝雀埋点清单完整、扫描算法与阈值写清
15. [ ] 资源/网络/文档白名单与题面一致；镜像 digest 固定
16. [ ] 难度标定完成，目标档位区间记录在案
17. [ ] 版本号与 replaces/retired_at 一致
18. [ ] notes.md 有构建/运行/验证命令与已知限制
19. [ ] 相对链接全部有效；外部事实有 URL
20. [ ] 文件清单符合 00-conventions §3；无二进制产物

## 11. 版本与生命周期

| 变更 | 版本 | trial 处理 | 榜单处理 |
| --- | --- | --- | --- |
| 文案/资源修正、错字、README | PATCH | 复用旧 trial | 同版本合并展示 |
| 新增/修改 rubric 条目、容差、隐藏测试断言 | MINOR | 重判（regrade），不重跑 Agent | 新旧版本分列或标注 |
| 改变验收语义（答案口径、硬门槛、规则族、生成器种子语义） | MAJOR | 全部重跑 | 不与旧版本直接比较 |

- **退役**：status=deprecated（保留成绩、停止新跑）→ retired（版本冻结）。退役题可公开题面与参考解（需 Lead 批准），隐藏测试永不公开；公开后不得再加入现役。
- **相似度剔除**：退役题公开后，对现役题做相似度检测，相似度超阈值的变体也退役或重构。
- **跨期可比性**：榜单只比较同一批次（同月）结果；跨版本比较必须注明版本与镜像 digest。
- 题目发现 bug 时：PATCH 复用旧 trial；MINOR 重判；MAJOR 重跑（与 00-conventions §2 一致）。
- **档位映射口径（跨榜单对照）**：参考项目若出现 A+/D+ 等扩展档，本方案**不扩枚举**，只做跨榜单粗略映射（A+→A、D+→D），映射表随报表注明，不进入 grade.tier 枚举。

## 12. 失败模式与验收标准

| 失败模式 | 表现 | 处置 |
| --- | --- | --- |
| 题目泄漏 | 工作区出现参考解/隐藏测试 | 立即下线；排查构建与镜像；金丝雀扫描 |
| 答案可猜 | 空 Agent 或随机猜测通过 | 提高硬门槛/参数化；空 Agent 必须 Failed |
| 测试过弱 | 硬编码/部分实现也过 | 变异测试不足 → 补隐藏测试；参考 EvalPlus 的额外测试思路 |
| 难度失准 | 最强模型 P、最弱模型也 P | 提高难度或退役（标定回填） |
| 预算超支 | 单题费用远超档位 | 收紧 max_total_tokens/max_wall_clock_s；拆题 |
| 平台不可用 | 设备掉线、VM 槽位泄漏 | 摘除设备/宿主；trial 作废重试（见 [04-isolation](04-isolation.md)） |
| 参考解跑不过 | 镜像升级/依赖漂移 | 锁定版本；参考解纳入 CI 每次跑 |

**验收标准（本文自身）**：30 题目录字段齐全且比例/配额可核对；元数据字段与 [task.schema.json](../schemas/task.schema.json) 逐条对应；预算/币种/隔离口径与 00-conventions 不冲突；流水线与闸门可执行；外部措辞有 URL；相对链接有效。

## 13. 外部引用（快照 2026-11-06）

- [LiveCodeBench（arXiv:2403.07974）](https://arxiv.org/abs/2403.07974)
- [SWE-bench+（arXiv:2410.06992）](https://arxiv.org/abs/2410.06992)
- [EvalPlus（arXiv:2305.01210）](https://arxiv.org/abs/2305.01210)
- [Min-K% Prob（arXiv:2310.16789）](https://arxiv.org/abs/2310.16789)
- [mutmut（Python 变异测试）](https://github.com/boxed/mutmut)
- [Harbor 文档索引](https://docs.harborframework.com/llms.txt)
- [Harbor：防奖励作弊公告](https://docs.harborframework.com/news/reward-hacking.md)
- [Apple 开发者论坛：虚拟化讨论](https://developer.apple.com/forums/activities/1586080)
- 内部： [00-conventions](00-conventions.md)、[03-grading](03-grading.md)、[04-isolation](04-isolation.md)、[06-answer-and-acceptance](06-answer-and-acceptance.md)、[task.schema.json](../schemas/task.schema.json)、[示例题包](../appendix/examples/)
