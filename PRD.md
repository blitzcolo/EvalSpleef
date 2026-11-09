# EvalSpleef · LLM Agentic 评测平台 · 项目需求规格（PRD）v3

| 项 | 内容 |
| --- | --- |
| 项目名 | **EvalSpleef**（CLI：`spleef`；Python 包前缀：`evalspleef`） |
| 文档版本 | v3.0-draft |
| 日期 | 2026-11-07 |
| 状态 | 待评审 → 评审通过后冻结为开工基线 |
| 变更说明 | 2026-11-09：Novita 相关需求已停用（discontinued）——`novita-dedicated`、`novita-serverless-gpu`、`novita-sandbox`，含 FR-SBX-04、M0 中的 Novita 项、AC-01 与 AC-03 中指名 Novita 的部分，不再排期、不再验证、不计入验收；代码保留，仍以 fake 跑测试。详见 `PLAN.md`「Discontinued: Novita」 |
| 配套文档 | `TECH-DESIGN.md`（技术与实现方案，与本文同目录） |
| 读者 | 评测负责人、平台工程师、模型接入/运维工程师、出题与标定人员 |
| 自包含说明 | 本文不依赖仓库内其他文档。v2 中继承下来的契约（枚举、状态机、档位算法、隔离不变量、题包契约等）以**原文照录**的方式放在附录 A–E；正文只写 v3 新增与变更的需求，并以「见附录 X」指向照录原文 |

---

## 0. 一页摘要

**我们评什么（不变）**：不是"模型答得对不对"，而是"在给定预算内，模型需要多少、什么级别的帮助（F0–F3），才能把真实任务做到验收线"。结果以 `扣分/档位(费用)` 的单元格进入榜单，档位由确定性脚本聚合，可复现、可重判、可审计。

**v3 新增了什么**：

1. **被测模型来源扩展为两类**：
   - 自部署模型：把 Hugging Face 上任意模型（公开、Gated、Private，含我们自己的微调版）部署到 **Novita AI Dedicated Endpoint**，作为被测对象。这是 v3 的首要需求。
   - 任何 **OpenAI 兼容接口**：官方厂商 API、Novita Serverless、自建 vLLM/SGLang、各类聚合平台。
2. **运行载体（沙箱）扩展为多种**：
   - **Novita AI Agent Sandbox**（云端托管）。
   - **自建 VPC**：E2B 自托管、K8s + gVisor/Kata。
   - **本地 Mac mini**：macOS VM、Linux 容器/VM。
   - **Linux 容器/虚拟机**。
   - **Windows 虚拟机/沙箱**。
   - v2 已规划的鸿蒙设备池与裸机性能机。
3. **统一抽象层 + 适配器**：模型侧一套 `ModelEndpointProvider` SPI，载体侧一套 `SandboxProvider` SPI。核心（调度、评测流、判定、隔离审计）只依赖这两套接口与能力声明。新增供应商只需写一个适配器，并通过一致性测试套件（conformance kit）。
4. **Harbor 从"主干"降为"适配器"**：评测内核自有。Harbor 以两种方式复用：
   - 作为外壳/验证/重判的执行适配器。
   - 把我们的沙箱适配器反向暴露成 Harbor 的 `BaseEnvironment`。

**不变的部分**：评测流状态机、F0–F3 反馈阶梯、档位算法（P…F 八档 + 半档 1/3 规则）、三层判分、隔离不变量 I1–I16、金丝雀协议、题包文件契约。v2 规格全部继承，原文见附录 A–E。

---

## 1. 背景

### 1.1 现状

v2 规格已经把评测流、判定、隔离、题库、答案五条主线写成可执行规格，并配有参考实现与 10 个示例题包。其架构以 Harbor 为 Agentic 主干，Inspect AI 跑逻辑赛道，macOS/鸿蒙/性能机自研 provider。被测模型默认通过各厂商官方 API 接入，由网关统一出口。

### 1.2 v3 的驱动因素

| # | 驱动 | 对系统的含义 |
| --- | --- | --- |
| D1 | 需要评测**自部署的 Hugging Face 模型**：开源模型、需要授权的 Gated 模型、团队私有或微调模型 | 系统必须管理"模型从哪来、哪个版本、部署在哪、端点是否就绪、能力是否满足题目要求、花了多少 GPU 钱"，而不只是"调哪个 API" |
| D2 | 选用 **Novita Dedicated Endpoint** 作为自部署的首选托管形态 | 适配它的实际能力边界：截至 2026-11-07，官方文档只描述控制台操作，**没有公开的部署管理 API**（见 §8.1） |
| D3 | 运行载体多样化：云沙箱（Novita）、自建 VPC、本地 Mac mini、Linux、Windows | 隔离、网络、文件传输、快照、并发上限在各载体上差异极大，必须用**能力声明**而不是假设来对齐 |
| D4 | 未来会持续新增模型供应商与沙箱供应商 | 供应商差异只能出现在适配器里；核心不出现任何供应商名 |
| D5 | Harbor 迭代快（约 3–4 周一个版本），且其抽象不覆盖我们的全部需求 | 不把内核焊死在 Harbor 上：它的 `TaskOS` 只有 linux/windows，没有 macOS；环境接口没有快照/重置；没有 Novita Sandbox 环境类型 |

### 1.3 与 v2 的差距（需要 v3 补齐的）

| 领域 | v2 状态 | v3 需求 |
| --- | --- | --- |
| 模型接入 | 只描述"网关 + 官方 API"；无模型注册表、无部署生命周期、无能力探测 | 模型注册表、HF 来源管理、端点生命周期、能力探测、自部署计费口径（FR-MOD） |
| 沙箱 | Harbor provider + 自研 Tart/鸿蒙；接口为 5 方法 | 统一 SPI + 能力协商 + 10 类适配器 + 本地主机代理 + 一致性套件（FR-SBX、FR-ABS） |
| Windows | 未覆盖 | Hyper-V VM 为主；Windows Sandbox 只用于开发自测（FR-SBX-08） |
| 架构主干 | Harbor | 自有内核，Harbor 为可选适配器（FR-ABS-07） |

---

## 2. 目标与非目标

### 2.1 目标（可度量）

| # | 目标 | 度量 / 验收口径 |
| --- | --- | --- |
| G1 | 任意 HF 模型（公开 / Gated / Private）可作为被测对象，经 Novita Dedicated Endpoint 部署并进入批次 | 从"在 models.yaml 写下 repo@revision"到"端点通过能力探测、可冻结进批次"：人工操作 ≤ 15 分钟，其余等待由系统自动轮询与校验 |
| G2 | 任意 OpenAI 兼容端点可作为被测、裁判或模拟用户模型 | 只写配置、不写代码即可接入；能力探测自动给出可用题目范围 |
| G3 | 同一题包无需修改即可在满足能力要求的任一载体上运行 | 同题在两种不同载体上（如 Docker 与 Novita Sandbox）的参考解都通过，空 Agent 都失败 |
| G4 | 新增一个沙箱供应商 ≤ 5 人日，新增一个模型供应商 ≤ 3 人日 | 以通过 conformance kit 为完成标准；核心代码零改动 |
| G5 | 隔离"可证明"：每个载体都有隔离画像与预检；金丝雀跨 trial 零命中 | 每期隔离体检报告按载体分节；不满足的不变量必须有书面豁免 |
| G6 | 成本可控、可对账 | 每 trial 的 token 费用与每批次的 GPU/沙箱费用可对账，误差 < 5% |
| G7 | 可复现 | 每个 grade 可追溯到模型权重修订（HF commit sha）、部署形态、推理引擎信息、载体与模板 digest、外壳版本、题包版本 |
| G8 | 本地起步、云端扩展 | 单台 Mac mini + Novita 账号即可跑通端到端最小闭环（M0）；扩到生产只加适配器与主机，不改核心 |

### 2.2 非目标

- 不做模型训练、微调与权重托管：权重放在 Hugging Face，部署交给 Novita 或自建推理服务。
- 不做公网多租户 SaaS、不做实时榜单：T+1 批量出分。
- 不承诺跨载体的**性能类指标可比**。帧率、耗时等只在指定的性能载体上测，其他载体只做功能验收。
- 不通过浏览器自动化去操作 Novita 控制台来"伪造"部署 API。见 §8.1 与待决策 Q1。
- 不在 v3 首期覆盖 iOS 真机与 Windows GUI 自动化题；接口预留，题库后续补。

---

## 3. 术语

| 术语 | 定义 |
| --- | --- |
| 被测模型（target） | 接受评测的模型；角色还有 `judge`（裁判）与 `user`（模拟用户） |
| 模型规格 ModelSpec | 描述"哪个模型、从哪来、哪个版本、怎么部署"的不可变记录 |
| 模型来源 ModelSource | `huggingface`（repo + revision + 访问级别）/ `vendor`（厂商托管模型 slug）/ `custom`（任意 OpenAI 兼容服务背后的模型） |
| 端点 Endpoint | 一个可调用的推理入口（base_url + model 名 + 凭证引用），由某个 ModelEndpointProvider 管理 |
| 模型供应商适配器 | 实现 `ModelEndpointProvider` SPI 的插件，如 `novita-dedicated`、`openai-compatible` |
| 管理模式 management_mode | 适配器对端点生命周期的掌控程度：`api`（全自动）/ `assisted`（人工在控制台操作，系统生成指引、校验并轮询）/ `external`（仅登记已有端点） |
| 能力探测 probe | 对端点自动跑的一组检查：工具调用、流式、上下文长度、推理内容等，产出 `ModelCapabilities` |
| Model Gateway | 所有模型流量的唯一出口：协议转换、per-trial 虚拟密钥、预算、计费、供应商锁定、日志脱敏 |
| 运行载体 / 沙箱 | 题目工作区与工具执行所在的隔离环境 |
| 沙箱供应商适配器 | 实现 `SandboxProvider` SPI 的插件，如 `novita-sandbox`、`docker`、`tart`、`hyperv` |
| 能力声明 capabilities | 适配器如实声明自己能做什么（OS、隔离级别、网络模式、快照、GUI、GPU、并发上限…），调度只按声明匹配 |
| 隔离画像 isolation profile | 某载体对 I1–I16 不变量的满足方式、检查命令与豁免项 |
| 主机代理 hostd | 部署在本地机器（Mac mini、Linux 宿主、Windows 宿主）上的守护进程，把本机载体以远程适配器形式暴露给控制面 |
| 模板 template | 载体侧的不可变基线（OCI 镜像 digest、Novita/E2B 模板 ID、Tart 金镜像、Windows 金 VHDX），每个 trial 都从它新建 |
| 执行模式 | `remote-tools`（外壳在沙箱外，工具调用经 SPI 进入沙箱）/ `in-sandbox`（外壳在沙箱内运行，模型流量只能出到网关） |
| conformance kit | 每个适配器必须通过的一致性测试套件 |

v2 的 batch / cell / trial / round / phase 等名词保持不变，见附录 B.1 与 A.1。

---

## 4. 角色与核心场景

### 4.1 角色

| 角色 | 主要诉求 |
| --- | --- |
| 评测负责人 | 定批次（模型 × 题目 × epoch）、批预算、审核发布；看到成本与隔离体检 |
| 模型接入工程师 | 把一个 HF 模型或 OpenAI 兼容端点变成"可被测"的端点；管部署、预热、缩容、下线 |
| 平台工程师 | 维护载体与适配器；接新供应商；处理宿主健康与事故 |
| 出题人 | 写题包；声明题目对载体的能力要求，而不是写死某个供应商 |
| 标定员 / 仲裁人 | 标定裁判、处理仲裁队列 |
| 榜单读者 | 看到每个单元格背后的模型来源（官方 API / 自部署 + HF 修订号）与可比性标注 |

### 4.2 用户故事（验收以此为准）

| ID | 故事 | 验收要点 |
| --- | --- | --- |
| US-01 | 作为模型接入工程师，我在 `models.yaml` 写入 `hf://org/model@<commit>`，访问级别为 `gated`，部署目标为 `novita-dedicated`。系统校验 HF 令牌能读该仓库且已接受许可，再生成一份控制台部署指引（模型 ID、GPU 推荐、副本与并发设置）。我在控制台完成部署后，把 Quick Start 中的 base_url 与 model 名贴回。系统随即自动轮询就绪、跑能力探测，并把端点标为 `ready` | 校验失败给出明确原因（无权限 / 未接受许可 / 非 safetensors）；贴回后 ≤ 10 分钟出探测报告 |
| US-02 | 作为模型接入工程师，我想全自动部署一个需要特定 vLLM 参数（如工具调用解析器）的模型 | 选择 `novita-serverless-gpu` 适配器（官方 REST API + vLLM 镜像）即可全自动创建、就绪、探测、销毁 |
| US-03 | 作为评测负责人，我把 `openai-compatible` 端点（如某厂商官方 API）与自部署端点放进同一批次 | 两类端点经同一网关计费与限流；榜单上标注来源与计费口径 |
| US-04 | 作为评测负责人，冻结批次前我需要知道"这个模型能跑哪些题" | 能力探测结果 × 题目要求 → 可运行题目清单；不满足的题自动 Skip 并写明原因（如"不支持工具调用"） |
| US-05 | 作为平台工程师，我把 Linux 题从本地 Docker 切到 Novita Agent Sandbox | 只改 `providers.yaml` 的路由，题包不变；参考解在两边都通过 |
| US-06 | 作为平台工程师，我在 Mac mini 上同时跑 2 个 macOS VM trial 和若干 Linux 容器 trial | 调度遵守每宿主 2 个 macOS VM 上限；宿主离线时其 trial 作废重排，不污染成绩 |
| US-07 | 作为平台工程师，我需要 Windows 题的运行载体 | Hyper-V VM 从金镜像差分盘新建、经 OpenSSH 执行并流式回传输出；用完即删 |
| US-08 | 作为平台工程师，我要接入一家新的沙箱供应商 | 实现 SPI + 注册插件 + 通过 conformance kit；核心零改动；≤ 5 人日 |
| US-09 | 作为评测负责人，批次开始前自部署端点要预热，批次结束要自动缩容/提示下线 | 批次状态 `Frozen → Running` 前所有端点 `ready` 且 warm；`Graded` 后按策略缩到 0 或提示人工 Terminate；GPU 空转费用可见 |
| US-10 | 作为审计者，我要能回答"这个分数是用哪个权重、哪个引擎、在哪个沙箱里跑出来的" | `trial.versions` 含模型来源 / 修订号 / 部署适配器 / 引擎信息（若可得）/ 载体与模板 digest |
| US-11 | 作为评测负责人，我想复用 Harbor 生态（外壳集成、Terminal-Bench 类任务、regrade） | Harbor 适配器可选启用；我们的沙箱适配器可被 `harbor run -e module:Class` 使用 |

---

## 5. 范围与优先级

优先级：**P0** = M0/M1 必须交付；**P1** = M2 交付；**P2** = M3 交付；**P3** = 预留接口，按需实现。里程碑定义见 §10。

### 5.1 模型侧适配器

| ID | 适配器 | 管理模式 | 用途 | 优先级 |
| --- | --- | --- | --- | --- |
| MP-1 | `openai-compatible` | external | 任意 OpenAI 兼容端点（官方 API、Novita Serverless、聚合平台、自建服务） | **P0** |
| MP-2 | `novita-dedicated` | assisted（若 Novita 提供 API 则升级为 api） | HF 模型（公开 / Gated / Private）部署到 Novita Dedicated Endpoint | **P0** |
| MP-3 | `novita-serverless-gpu` | api | 用 Novita Serverless GPUs v2 REST API + vLLM 官方镜像全自动部署；需要自定义引擎参数时使用 | P1 |
| MP-4 | `vpc-vllm`（vLLM/SGLang 自建，K8s 或 GPU 实例） | api | 数据不出 VPC、或需完全掌控引擎版本时使用 | P2 |
| MP-5 | 厂商原生协议（Anthropic Messages / OpenAI Responses）直连 | external | 原生外壳需要的协议；也可由网关转换 | P0（经网关） |

### 5.2 运行载体适配器

| ID | 适配器 | 载体 | OS | 隔离级别（附录 D.1） | 优先级 |
| --- | --- | --- | --- | --- | --- |
| SP-1 | `docker`（runc / runsc） | 本地或自建 Linux 宿主 | Linux | L1 / L2 | **P0** |
| SP-2 | `novita-sandbox` | Novita AI Agent Sandbox | Linux（+ 桌面模板） | L3（官方未公开实现，见 §8.2；按 L3 申报需经验证） | **P0** |
| SP-3 | `tart` | Mac mini（Apple Silicon） | macOS / Linux VM | L3 | P1 |
| SP-4 | `e2b-selfhost` | 自建 VPC（E2B 开源基础设施，Firecracker） | Linux | L3 | P1 |
| SP-5 | `hyperv` | Windows 宿主（Hyper-V） | Windows / Linux VM | L3 | P1 |
| SP-6 | `harbor-bridge` | 双向：Harbor 环境 ↔ 我们的 SPI | 视下游而定 | 视下游而定 | P1 |
| SP-7 | `libvirt`（QEMU/KVM） | 自建 Linux 宿主 | Linux / Windows VM | L3 | P2 |
| SP-8 | `apple-container` | Mac mini（macOS 26+） | Linux 轻量 VM | L3 | P2 |
| SP-9 | `device-pool` / `perf-host` | 鸿蒙设备池、裸机性能机（继承 v2） | HarmonyOS / macOS 裸机 | L4 | P2 |
| SP-10 | `k8s-agent-sandbox`（kubernetes-sigs/agent-sandbox + gVisor/Kata） | 自建 VPC K8s | Linux | L2 / L3 | P3 |
| SP-11 | `windows-sandbox` | Windows 11 宿主 | Windows | L3 | P3（仅开发自测，不进正式批次，见 §8.4） |

> 云沙箱供应商（E2B 托管、Daytona、Modal 等）不在首期范围；接口保证可加，作为 G4 的验证样例。

---

## 6. 功能需求

编号规则：`FR-<域>-<序号>`；每条需求写明"必须 / 应当 / 可以"（MUST / SHOULD / MAY）。

### 6.1 模型接入（FR-MOD）

**FR-MOD-01 模型注册表（MUST）**

系统维护模型注册表，每条 `ModelSpec` 不可变，修改即新版本。必填字段：

- `slug`：沿用附录 A.1 规则。
- `role_allowed`：取 `target` / `judge` / `user`。
- `family`：用于裁判反同源规则，见附录 C.5。
- `source`：类型 `huggingface` / `vendor` / `custom`。
- `deployment`：适配器名与参数。
- `pricing`：计费口径与价格表版本。
- `license`。
- `owner`。

**FR-MOD-02 Hugging Face 来源（MUST）**

1. 支持三种访问级别：`public` / `gated` / `private`。
2. **修订号必须固定到 commit sha**。冻结批次时记录 sha，批次结束时复核仍可解析到同一 sha。不允许只写分支名，例如 `main`。
3. **访问预检**：用部署时将使用的同一 HF 身份，在登记时自动检查四项：
   - 仓库可读；
   - Gated 模型已接受许可；
   - 权重为 `.safetensors`（Novita Dedicated Endpoint 不支持 `.bin`，见 §8.1）；
   - `config.json` 中的架构字段存在。
   
   任一失败即拒绝登记，并给出明确原因。
4. **不可变性保障**：部署平台在扩容或重拉时会重新下载权重，而 Novita 控制台未提供修订号字段。因此对于 `novita-dedicated`，系统 SHOULD 采用"冻结镜像仓库"策略：把指定 commit 同步到我们组织下的私有 HF 仓库，并对该仓库禁止推送，部署指向这个镜像仓库。详见 TECH-DESIGN §6.5。
5. **令牌管理**：HF 令牌使用最小权限的细粒度只读令牌，只授权到所需仓库。令牌存于密钥管理，不写入任何配置文件或日志。若令牌失效，Novita 会在重拉时失败，因此令牌有效期必须覆盖批次周期，系统在批次冻结时检查有效期。
6. 记录 `chat_template` 与 tokenizer 配置文件的哈希，作为可复现字段。

**FR-MOD-03 Novita Dedicated Endpoint 部署（MUST）**

适配器 `novita-dedicated` 采用 `assisted` 管理模式。流程与状态：

```
registered ──预检通过──▶ awaiting_operator ──贴回 base_url/model──▶ verifying ──探测通过──▶ ready
     │                         │（系统生成部署指引）                     │                       │
     ▼                         ▼                                       ▼                       ▼
  rejected                  expired(超时 24h)                       probe_failed          warm / sleeping / retired
```

1. **部署指引**：系统生成控制台部署所需的全部信息，包括：
   - 部署名：带批次与模型 slug。
   - 模型 ID：HF repo，或冻结镜像 repo。
   - 是否需要 HF Token。
   - 推荐 GPU：基于参数量、精度、上下文长度的显存估算。
   - Min/Max Replicas。
   - Max Concurrency per Replica：Novita 默认 16。
   - Scale-down Delay：Novita 最小 300 s。
   - 是否开启 Suffix Decoding：首期一律关闭，避免影响采样分布。
2. **贴回与校验**：操作员贴回 Quick Start 中的 base_url、model 名，以及控制台的部署 ID。系统随即：
   - 调用 `/models`（若可用）与一次最小 chat 请求，确认可达、鉴权正确、model 名匹配；
   - 进入能力探测（FR-MOD-05）。
3. **状态推断**：由于没有管理 API，系统只能由推理面推断端点状态。规则：
   - 请求成功 → `warm`；
   - 首请求耗时 > 60 s 或返回特征性等待 → 视为从 `SLEEPING` 冷启动。Novita 文档写明冷启动约 5 分钟。
   - 记录冷启动事件，冷启动期间的耗时不计入 trial 墙钟（见 FR-MOD-06）。
4. **升级路径**：若 Novita 后续提供部署管理 API，只需在该适配器内实现 `deploy / status / scale / teardown`，管理模式升级为 `api`，核心不变。
5. **LoRA**：支持"同一部署多 LoRA、请求时用 `model` 字段选择适配器"。每个 LoRA 作为独立 `ModelSpec`（source 指向 base + adapter repo@sha），共享同一端点。P1。

**FR-MOD-04 OpenAI 兼容通用接入（MUST）**

- 配置项：`base_url`、`model`、凭证引用、可选请求头、协议（`chat_completions` / `responses`）、限流参数。
- 适用于：
  - 官方厂商 API；
  - Novita Serverless LLM API（`https://api.novita.ai/openai`）；
  - 自建 vLLM/SGLang；
  - 聚合平台。
- 聚合平台 MUST 开启"供应商锁定"并校验响应中的实际供应商与模型字段；不一致的 trial 按 provider 类错误作废（继承附录 B.6）。

**FR-MOD-05 能力探测（MUST）**

每个端点在进入批次前必须跑探测套件，产出 `ModelCapabilities` 与探测报告。探测结果写进批次冻结记录。探测项至少包括：

| 探测项 | 判定 | 影响 |
| --- | --- | --- |
| 基础 chat、流式 SSE、`usage` 返回 | 必须通过 | 不通过不可用 |
| 工具调用：单个、并行、`tool_choice` 强制、多轮工具结果回填 | 记录通过级别 | 不支持工具调用 ⇒ 只能跑逻辑赛道 |
| 结构化输出（`json_schema` / `json_object`） | 记录 | 裁判 / 模拟用户角色需要 |
| 推理内容（`reasoning_content` 或等价字段）与开关 | 记录 | 影响外壳配置与 token 计费 |
| 有效上下文长度（二分探测，至 ModelSpec 声明值） | 实测值 ≥ 题目要求 | 不满足的题自动 Skip |
| 最大输出长度、`stop`、`seed`、温度生效 | 记录 | 复现性字段 |
| 吞吐与 TTFT（并发 1/4/16） | 记录 | 用于计算批次所需副本数 |
| 协议转换：经网关以 Anthropic Messages / OpenAI Responses 调用 | 记录 | 决定可用的原生外壳 |
| 模型身份：响应 `model` 字段、system fingerprint（若有） | 必须与登记一致 | 防路由漂移 |

对 Novita Dedicated Endpoint，"工具调用、logprobs、推理内容是否可用"未见官方文档说明（§8.1），**必须以探测结果为准**。

**FR-MOD-06 端点生命周期与批次联动（MUST）**

1. 批次 `Frozen → Running` 的守卫条件：批次内所有自部署端点处于 `ready`，并已预热到所需副本数。对 `assisted` 模式，系统提醒操作员把 Min Replicas 调到计划值。
2. 批次运行中：
   - 端点进入 `SLEEPING` 或不可达时，派发暂停，已在跑的 trial 按 provider 类错误处理；
   - 冷启动等待由网关吸收，不计入 trial 墙钟，但计入事件日志。
3. 批次 `Graded` 后，按策略执行以下之一：
   - 缩到 0（Scale-to-Zero，SLEEPING 不计费）；
   - 提示人工 Terminate；
   - 保留供重判。
   
   重判不需要被测端点，只需要裁判端点。
4. 端点空转（RUNNING 但无流量）超过阈值时告警，因为 Novita 文档写明 RUNNING 状态即使零流量也计费。

**FR-MOD-07 Model Gateway（MUST）**

所有角色（target / judge / user）的模型流量 MUST 经过网关。网关能力：

1. **统一入口与协议转换**：对外提供以下兼容接口，转换到后端端点的实际协议：
   - OpenAI Chat Completions；
   - OpenAI Responses（P1）；
   - Anthropic Messages。
   
   自部署开源模型一般只提供 Chat Completions。原生外壳（如 Claude Code、Codex CLI）需要各自协议，由网关转换。
2. **per-trial 虚拟密钥**：
   - 每个 trial 一个短期密钥，绑定唯一端点与预算上限，失效时间为 trial 最大墙钟加缓冲；
   - 长期密钥（Novita key、厂商 key、HF token）只在网关与密钥管理中。
   
   继承附录 D.4。
3. **预算强制**：每次请求前检查 token 与费用余额，超限拒绝并返回 `cost_cap` / `token_cap`。继承附录 B.5。
4. **重试与限流**：
   - 对 429/5xx 指数退避；
   - 对同一 trial 不跨端点故障转移，因为换端点等于换被测对象。
5. **记录**：每请求的 trial_id、round、端点、实际 model 字段、token 用量、耗时、冷启动标记、状态码。默认不持久化请求正文；正文作为轨迹的一部分，按轨迹保留策略存储并脱敏。
6. **网关自身升级视为版本变更**：网关版本写入 `trial.versions`。

**FR-MOD-08 计费口径（MUST）**

| 来源 | 主口径（进榜单单元格） | 辅口径（进成本报告） |
| --- | --- | --- |
| 官方 API / Serverless | token 用量 × 冻结价格表（附录 B.5） | 同主口径 |
| 自部署（Novita DE、Serverless GPU、VPC） | token 用量 × **参考单价**。默认取同模型在 Novita Serverless 的公开价；无公开价时由评测负责人在价格表中指定并标注 `assumed` | 实际 GPU 费用：GPU 秒 × 单价，按批次分摊到 trial（按 token 占比） |

榜单单元格费用 MUST 标注口径：`api` / `ref-price` / `assumed`。两种口径不可直接比较的，榜单分栏或加脚注。见待决策 Q3。

**FR-MOD-09 版本冻结（MUST）**

每个 trial 的 `versions` 在 v2 字段基础上新增以下字段：

- `model_source`：来源类型；
- `hf_repo`；
- `hf_revision`；
- `hf_mirror_repo`：可空；
- `deployment_adapter`；
- `deployment_id`；
- `endpoint_base_url_hash`；
- `inference_engine`：可得时填写，如 vllm/sglang 及版本；不可得记 `unknown`；
- `gpu_type`；
- `replicas_planned`；
- `quantization`；
- `context_length_effective`；
- `probe_report_id`；
- `gateway_version`；
- `chat_template_sha256`。

**FR-MOD-10 角色与反同源（MUST）**

- 裁判与模拟用户模型也经注册表与网关接入，并接受同样的能力探测，重点是结构化输出与稳定性。
- 裁判反同源规则（附录 C.5）以 `family` 字段判定。自部署微调模型的 `family` 取其基座模型家族。

### 6.2 运行载体（FR-SBX）

**FR-SBX-01 能力声明与匹配（MUST）**

1. 每个载体适配器 MUST 声明能力，包括：
   - OS 家族、CPU 架构、隔离级别；
   - 支持的网络模式及允许列表类型（域名 / 通配 / IP / CIDR）、是否可运行期切换网络策略；
   - 流式输出、快照（无 / 文件系统 / 内存）、快照一对多、暂停恢复；
   - GUI、GPU、宿主目录挂载、资源限额是否强制；
   - 并发上限、单实例最长寿命。
2. 题包以 `runtime.requirements` 声明需求，包括 OS、架构、最低隔离级别、网络模式、GPU、GUI、资源与时长。见 FR-TSK-01。
3. **调度只按"需求 ⊆ 能力"匹配**。批次冻结时，任何"无载体可满足"的题直接 Skip 并写明原因。
4. 不允许静默降级：载体无法强制所请求的网络策略时，在创建阶段拒绝，不得退回"全联网"。

**FR-SBX-02 生命周期（MUST）**

1. 每个 trial 从不可变模板新建一个载体实例，用完即毁，不回池。继承附录 D.2 中的 I1、I14。
2. 必需操作：
   - 创建；
   - 执行命令：支持 cwd/env/user/超时，有退出码，stdout/stderr 流式或批量；
   - 上传 / 下载文件与目录；
   - 销毁：必须幂等。
3. 可选操作（按能力）：
   - 快照与从快照创建；
   - 暂停恢复；
   - 运行期改网络策略；
   - GUI 端点（VNC/RDP）；
   - 端口暴露。
4. 快照只可用于"同一模板的预置加速"，例如题目依赖装好后的基线快照。不得用于把跑过 trial 的状态复制给另一个 trial。
5. 所有实例 MUST 打标签：batch / trial / lease / provider，并设置 TTL。系统周期性扫描孤儿实例，强制回收并告警。

**FR-SBX-03 网络策略（MUST）**

- 题包的四种网络模式 `off` / `docs_whitelist` / `package_mirror` / `full`（附录 D.3）映射到各载体能力。
- 载体原生只支持 CIDR、或只支持开关时，系统 MUST 提供**共享出口代理**：载体只允许访问代理地址，由代理按域名执行允许列表、只读镜像与留痕。日志字段继承附录 D.3（I16）。
- `in-sandbox` 执行模式下，被测外壳访问模型的唯一出口是网关域名，网关域名自动加入允许列表。

**FR-SBX-04 Novita AI Agent Sandbox（MUST）**

1. 使用官方 SDK（`novita-sandbox` 2.x），区域默认 `us-phx-1`（v2，支持快照与 Secrets）。
2. 模板：
   - 从题目的 OCI 镜像（固定 digest）构建 Novita 模板，CPU 与内存在模板上设定；
   - 模板 ID 与镜像 digest 的映射写入模板注册表。
3. 网络：
   - `off` 用 `allow_internet_access=False`；
   - 允许列表用 `network.allow_out` / `deny_out`，支持域名、通配、IP、CIDR。
4. 生命周期：
   - 创建时设置 `timeout`，即最长寿命。SDK 默认仅 5 分钟，**必须显式设置**；
   - trial 结束调用 kill；
   - 不使用 `on_timeout: pause` / `auto_resume`，以免实例被意外复用。
5. 配额约束：付费档并发 100、单会话 ≤ 3 h、≤ 8 vCPU / 8 GiB、磁盘 20 GB（以官方文档为准，见 §8.2）。超出的题（如重型题墙钟 10800 s 已贴近上限）不路由到该载体。
6. 隔离：
   - Novita 未公开隔离实现。上线前 MUST 用 conformance kit 的隔离子集（附录 D.2 中适用条目）实测并写隔离画像；
   - 模板 MUST 去除 sudo，并以非 root 用户运行 Agent。

**FR-SBX-05 自建 VPC（SHOULD，P1）**

- 首选 E2B 开源基础设施自托管（Firecracker microVM，Apache-2.0）。它与 Novita 同属 E2B 系 SDK 形态，可共享适配器基类。
- 备选 K8s + kubernetes-sigs/agent-sandbox + gVisor/Kata（P3）。
- 要求：控制面与 VPC 之间专线或 VPN；沙箱不可访问 VPC 内其他网段与云元数据地址。

**FR-SBX-06 本地 Mac mini（MUST for macOS 题，P1）**

1. macOS 题用 Tart：每 trial 从金镜像克隆、`tart exec`（Guest Agent）或 SSH 执行，用完删除。
2. **每宿主并发 ≤ 2 个 macOS VM**（Apple 许可与 Virtualization.framework 双重限制，见 §8.3）。Linux 容器/VM 不占此额度，但与它们共享 CPU/内存。
3. Linux 题在 Mac mini 上可用 Docker（SP-1）或 apple/container（SP-8）。
4. Mac mini 通过 hostd 主动外连控制面，不开放入站端口。
5. 性能类验收不在 VM 内做，只在裸机性能机做（继承 v2）。

**FR-SBX-07 Linux 容器 / 虚拟机（MUST）**

- 默认 Docker/Podman（runc，L1），按题目需要升 gVisor（runsc，L2）。
- 需要独立内核或 Windows/GUI guest 时用 libvirt/QEMU（L3，P2）。
- 资源与网络加固项按附录 D.2 的 I2–I7 实施并预检。

**FR-SBX-08 Windows（SHOULD，P1）**

1. 正式批次使用 **Hyper-V VM**：
   - 从金镜像差分 VHDX 新建，或用检查点恢复到干净状态；
   - 通过 OpenSSH Server 执行命令，取得流式输出与退出码；
   - 文件经 SCP/SFTP 传输；
   - 用完删除差分盘。
2. **Windows Sandbox 不进正式批次**：同一宿主只能一个实例；CLI `wsb exec` 只返回退出码、不回传输出。只作为出题人本机自测。
3. Windows 的隔离预检需要 Windows 版探针集，如非管理员用户、无共享文件夹、防火墙出站策略，见 TECH-DESIGN §7.7。
4. 许可：Windows guest 的许可由平台工程师确认（Server Datacenter / 评估版 / VDA），写入宿主清单。

**FR-SBX-09 设备池与性能机（继承 v2，P2）**

- 鸿蒙设备池与裸机性能机作为 `L4` 载体接入同一 SPI。
- "设备租约 + 归还卫生"按附录 D.2 中的 I14 执行。

**FR-SBX-10 主机代理 hostd（MUST for 本地载体）**

- 本地机器上的载体经 hostd 接入控制面：
  - 主动外连（mTLS）；
  - 上报库存与槽位、心跳；
  - 执行适配器调用并回传流式输出；
  - 支持排空（drain）与维护模式。
- 心跳丢失 > 60 s 的宿主自动摘除，其 trial 按 environment 类错误作废重排（继承附录 B.6）。

**FR-SBX-11 隔离预检与画像（MUST）**

- 每个载体适配器 MUST 提供：
  - 隔离画像：对 I1–I16 逐条说明满足方式与检查命令，不满足的写豁免理由并由评测负责人批准；
  - 按 OS 家族的预检探针集。
- 预检失败的处置继承附录 B.2a（S5）。

### 6.3 统一抽象与扩展性（FR-ABS）

| ID | 需求 | 级别 |
| --- | --- | --- |
| FR-ABS-01 | 模型侧与载体侧各一套 SPI（`ModelEndpointProvider` / `SandboxProvider`），接口签名见 TECH-DESIGN §6.1、§7.1 | MUST |
| FR-ABS-02 | 适配器以插件形式注册（Python entry points），按名字在配置中引用；同一适配器可多实例（如两个 Novita 账号、三台 Mac mini） | MUST |
| FR-ABS-03 | conformance kit：每个适配器 MUST 通过其能力声明对应的全部用例（功能、错误语义、超时、幂等、并发、隔离子集），CI 中以 Mock 与真实环境两档运行 | MUST |
| FR-ABS-04 | 核心代码中不得出现任何供应商名或供应商 SDK 的 import；由静态检查规则强制 | MUST |
| FR-ABS-05 | 能力缺失时 SPI 抛出明确的 `CapabilityNotSupported`，调用方必须事先查询能力，不得 try/except 兜底降级 | MUST |
| FR-ABS-06 | 远程透明：本地适配器经 hostd 远程调用时，对核心呈现为同一 SPI（RemoteProvider 代理） | MUST |
| FR-ABS-07 | Harbor 双向适配：① `harbor-bridge` 把任意我们的沙箱适配器包装成 Harbor `BaseEnvironment`（供 `harbor run -e module:Class`）；② 可选用 Harbor 作为外壳与验证执行器（复用其外壳集成、`harbor job regrade`、`harbor analyze`） | SHOULD |
| FR-ABS-08 | 新增供应商工作量：沙箱 ≤ 5 人日、模型 ≤ 3 人日（含 conformance 通过与文档） | SHOULD |

### 6.4 Agent 外壳与执行模式（FR-RUN）

1. **两种执行模式（MUST）**：
   - `remote-tools`：外壳与 LLM 调用在沙箱外的 Runner 进程，工具调用通过 SPI 的 `exec`/文件接口进入沙箱。沙箱可完全断网。统一外壳的默认模式。
   - `in-sandbox`：外壳（如 Claude Code、Codex CLI、OpenCode）安装在模板内、在沙箱内运行。沙箱唯一允许的模型出口是网关，凭证是 per-trial 短期虚拟密钥。原生外壳的默认模式。
2. **外壳矩阵（MUST）**：
   - 原生外壳与统一外壳双轨，榜单分栏（继承 v2）；
   - 自部署开源模型默认只跑统一外壳，若要跑某原生外壳，须通过该外壳协议的能力探测（FR-MOD-05）；
   - 外壳版本固定并写入 `trial.versions`。
3. 外壳停止原因、心跳、循环检测继承附录 B.3。

### 6.5 评测流（FR-FLOW）

继承附录 B 全部内容（批次流程、trial 状态机与合法转移、租约与预检、停止条件、预算、异常矩阵、cell 状态、事件日志）。v3 新增：

| ID | 需求 |
| --- | --- |
| FR-FLOW-01 | 批次冻结时额外冻结：端点探测报告 ID、模型修订号、载体模板 digest 映射、网关版本；任何一项变化必须开新批次 |
| FR-FLOW-02 | 批次 `Frozen → Running` 的新增守卫：自部署端点 `ready + warm`；所需载体容量（并发 × 时长）可满足；载体隔离画像有效 |
| FR-FLOW-03 | 同一 trial 的所有轮次 MUST 在同一载体实例、同一端点上完成；载体或端点在 trial 中途失效 ⇒ trial `failed_infra` → 新 trial 重跑（不跨端点/载体续跑） |
| FR-FLOW-04 | 新增事件类型：`endpoint.registered`、`endpoint.probe.result`、`endpoint.warm`、`endpoint.cold_start`、`endpoint.unavailable`、`endpoint.retired`、`provider.capability.reject`、`host.drained`、`sandbox.orphan.reaped` |
| FR-FLOW-05 | 错误分类沿用 8 类（附录 A.2 §4.6）；新增映射：端点不可用/冷启动超时 → `provider`；载体创建失败/宿主离线/配额耗尽 → `environment`；能力探测与实际不符（如探测通过但运行时工具调用解析失败） → `provider` + 人工复核 |
| FR-FLOW-06 | 冷启动与载体排队时间不计入 trial 墙钟预算，但记录在事件日志与成本报告 |

### 6.6 判定（FR-GRD）

继承附录 C 全部内容（三层判分、硬门槛短路、F0–F3 反馈阶梯、档位算法、扣分、裁判面板）。v3 新增：

- 裁判与模拟用户模型可来自任一模型适配器，但必须通过结构化输出与稳定性探测。裁判模型版本与端点冻结进批次。
- 自部署裁判（如开源大模型做裁判）MUST 满足反同源规则，且一致性标定单独进行（附录 C.5 的阈值不变）。

### 6.7 隔离（FR-ISO）

继承附录 D 全部内容（隔离强度分级、I1–I16、网络策略、密钥与脑手分离）。v3 新增：

| ID | 需求 |
| --- | --- |
| FR-ISO-01 | 每个载体适配器的隔离画像是发布物的一部分；每期隔离体检报告按载体分节 |
| FR-ISO-02 | 云托管载体（Novita Sandbox）：Agent 沙箱内不得出现 Novita API key；沙箱创建/销毁凭证只在控制面 |
| FR-ISO-03 | `in-sandbox` 模式下沙箱内唯一凭证是 per-trial 网关虚拟密钥：绑定单 trial、单端点、预算上限、到期自动失效；任何长期凭证进入沙箱 = 事故（附录 D.4） |
| FR-ISO-04 | 金丝雀令牌注入与跨 trial 扫描覆盖所有载体（含 Windows、macOS VM） |
| FR-ISO-05 | 模型服务端状态隔离：自部署端点只服务评测流量（不与其他业务共用部署）；关闭可能存在的服务端会话/缓存跨请求复用的功能（如前缀缓存不影响正确性，可保留，但需记录） |

### 6.8 题库（FR-TSK）

继承附录 E 全部内容（题包文件契约、三层分类与 12 track、元数据字段）。v3 新增：

**FR-TSK-01 运行需求块（MUST）**

`task.yaml` 新增 `runtime.requirements`，取代"在 `platform` 里写死载体"的做法。`platform` 字段保留用于兼容与展示。

```yaml
runtime:
  requirements:
    os: linux            # linux | macos | windows | harmony
    arch: [x86_64, arm64]
    min_isolation: L1    # L1..L4，见附录 D.1
    network: off         # off | docs_whitelist | package_mirror | full
    gpu: false
    gui: false
    cpus: 4
    memory_mb: 8192
    disk_mb: 20480
    max_lifetime_s: 5400 # ≥ 预算墙钟 + 观察 + 余量
  environment:
    oci_image: registry.local/llm-eval/web-001@sha256:...   # Linux 题的唯一事实来源
    macos_image: null                                      # macOS 题：Tart 金镜像 OCI 引用 + digest
    windows_image: null                                    # Windows 题：金 VHDX 标识 + sha256
  execution_modes: [remote-tools, in-sandbox]              # 本题允许的执行模式
  model_requirements:
    tool_calling: required   # required | optional | none
    min_context_tokens: 64000
```

**FR-TSK-02**：载体相关的特殊参数只能放在 `runtime.provider_overrides.<adapter>` 下，并标注"仅优化、不改变语义"。

**FR-TSK-03**：题包 lint 新增规则：
- `runtime.requirements` 必填；
- 镜像必须带 digest；
- `max_lifetime_s` ≥ `budget.max_wall_clock_s + max_rounds × max_observe_s`；
- 声明 `in-sandbox` 的题必须声明网关出口。

### 6.9 报表与榜单（FR-RPT）

1. 单元格格式不变：`扣分/档位(费用)`（附录 A.2 §4.4）。
2. 单元格下钻 MUST 展示：
   - 模型来源（官方 API / 自部署 + HF repo@sha 短号）；
   - 部署适配器；
   - 引擎信息（可得时）；
   - 载体适配器与隔离级别；
   - 费用口径。
3. 同一批次中，自部署模型与官方 API 模型可同表，但费用列按口径分栏或加脚注。
4. 每期发布物 = 榜单 + 隔离体检报告（按载体分节）+ 判定一致性报告 + 版本清单 + **端点探测报告汇总** + **成本对账报告**（token 费用 vs 实际 GPU/沙箱账单）。

### 6.10 运维、成本与可观测（FR-OPS）

| ID | 需求 |
| --- | --- |
| FR-OPS-01 | 统一的 CLI：`spleef models register/probe/warm/retire`、`spleef providers list/check/conformance`、`spleef hosts list/drain`、`spleef batch plan/freeze/run/report` |
| FR-OPS-02 | 成本仪表：按批次显示 token 费用（实时）、GPU 端点运行时长（自部署）、沙箱 vCPU·s/GiB·s（Novita）、本地主机占用；预计 vs 实际 |
| FR-OPS-03 | 告警：端点空转、端点不可用、冷启动频发、孤儿沙箱、宿主心跳丢失、配额将尽、HF 令牌临期 |
| FR-OPS-04 | 所有供应商账单数据可导入并与事件日志对账（Novita 账单 API 支持按 `productCategory` 过滤，见 §8.1） |

---

## 7. 非功能需求

| ID | 类别 | 需求 | 度量 |
| --- | --- | --- | --- |
| NFR-01 | 可复现 | 同一 trial 记录重算 grade 结果一致（regrade 幂等）；同配置重跑档位差 ≤ 1 档的比例 ≥ 90%（继承 v2） | 回归测试 + 每期抽样 |
| NFR-02 | 可审计 | 任一单元格可下钻到：轨迹、证据、裁判输入快照、模型修订、端点探测报告、载体隔离画像 | 抽查 10 个单元格全部可下钻 |
| NFR-03 | 安全 | 长期凭证零入沙箱；HF/Novita/厂商密钥只在密钥管理与网关；日志脱敏抽检命中 0 | 每期密钥扫描 |
| NFR-04 | 吞吐 | M2 目标：单批 720 trial（8 模型 × 30 题 × 3 epoch）在 ≤ 24 h 内完成；Linux 侧并发 ≥ 46 路（v2 容量公式） | 实测 |
| NFR-05 | 可用性 | 单载体或单端点故障不影响其他 trial 的有效成绩；受影响 trial 自动作废重排 | 故障注入演练 |
| NFR-06 | 启动延迟 | 载体实例从请求到可执行：Novita/Docker ≤ 30 s（P95），Tart macOS ≤ 120 s，Hyper-V ≤ 180 s（目标值，P1 实测后校准） | 指标面板 |
| NFR-07 | 扩展性 | 见 G4；核心与适配器版本独立发布 | conformance kit 通过即可上线 |
| NFR-08 | 成本 | 批次费用预估误差 < 5%；端点空转费用占自部署 GPU 费用 ≤ 15% | 成本对账报告 |
| NFR-09 | 可移植 | 控制面可在单台机器（含 Mac mini）以 docker compose 运行（M0），也可在 K8s 运行（M2+） | 两种部署均通过冒烟 |
| NFR-10 | 可观测 | 所有 SPI 调用带 trace（OpenTelemetry），可按 trial 串联 | 抽查 |

---

## 8. 外部约束与已知事实（2026-11-07 快照，开工前需复核）

> 下列事实来自官方文档或源码，已附 URL；标【待核实】的为文档未说明或互相矛盾、需要向供应商确认或实测的项。

### 8.1 Novita AI Dedicated Endpoint

| 事实 | 对设计的影响 | 来源 |
| --- | --- | --- |
| 模型来源：Novita 目录模型，或 HF repo ID；Private/Gated 需在控制台点 "Integrate HF Token" 绑定令牌；令牌过期/吊销会导致后续重拉失败 | 访问预检 + 令牌有效期检查 + 冻结镜像策略（FR-MOD-02） | <https://docs.novita.ai/guides/llm-dedicated-endpoint> |
| 自定义/微调权重需放在 HF；必须 `.safetensors`，`.bin` 不支持 | 登记时校验文件格式 | 同上 |
| 引擎为 vLLM 或 SGLang，"按模型自动匹配"；对外仅暴露 Max Concurrency per Replica（默认 16）与 Suffix Decoding | 无法指定工具调用解析器等引擎参数 ⇒ 工具调用能力必须探测；需要定制时走 MP-3（`novita-serverless-gpu`） | 同上 |
| 状态：PENDING → DEPLOYING（Requesting GPU / Downloading Model / Engine Initializing）→ RUNNING；另有 FAILED、SLEEPING、ROLLING、SCALING、TERMINATING、TERMINATED；部署通常 5–60 分钟 | 适配器状态机与超时设置 | 同上 |
| 自动扩缩：Min Replicas 默认 1（0 即 Scale-to-Zero），Max 默认 3，Scale-down Delay 默认且最小 300 s；SLEEPING 冷启动通常约 5 分钟 | 批次预热与冷启动吸收（FR-MOD-06） | 同上 |
| 计费按 GPU·秒；RUNNING 即使零流量也计费，SLEEPING/TERMINATED 不计费；"何时开始计费"文档内前后矛盾【待核实】 | 空转告警；成本对账 | 同上 |
| GPU 类型创建后不可修改，需删除重建 | 部署指引要先做显存估算 | 同上 |
| 调用方式：OpenAI 兼容（纯文本推理），base_url 与 model ID 从控制台 Quick Start 复制；文档未给出 URL 格式。2025-07 博客给出 `https://api.novita.ai/dedicated/v1/openai`【待核实是否仍为规范 URL】 | base_url 由操作员贴回，不硬编码 | 同上；<https://blogs.novita.ai/deploy-custom-base-model-with-llm-dedicated-endpoint/> |
| **API 参考中没有 Dedicated Endpoint 的创建/查询/扩缩/删除接口**；只有账单 API 支持 `productCategory=llm_dedicated_endpoint` | 适配器采用 `assisted` 模式；向 Novita 确认是否有未公开 API（待决策 Q1） | <https://docs.novita.ai/llms.txt> |
| 工具调用、logprobs、reasoning 在 Dedicated Endpoint 上是否可用：文档未说明【待核实】（Serverless LLM API 文档有这些参数） | 以能力探测为准 | 同上；<https://docs.novita.ai/api-reference/model-apis-llm-create-chat-completion> |
| 价格（产品页）：H100 SXM 80GB $1.99/GPU·h、H200 141GB $2.99/GPU·h、RTX 4090 $0.61/GPU·h（同页另有与之矛盾的按秒价【待核实】） | 成本估算 | <https://novita.ai/dedicated-endpoint> |
| 全自动替代：Serverless GPUs v2 REST API（`POST /gpus/v2/endpoints`，可指定镜像、环境变量、副本、健康检查），官方示例用 `vllm/vllm-openai` 镜像与 `HUGGING_FACE_HUB_TOKEN` 环境变量 | MP-3 适配器 | <https://docs.novita.ai/api-reference/gpus-v2-create-endpoint>、<https://docs.novita.ai/guides/serverless-gpus-quickstart-create-endpoint> |

### 8.2 Novita AI Agent Sandbox

| 事实 | 对设计的影响 | 来源 |
| --- | --- | --- |
| SDK：`pip install novita-sandbox`（2.x，最新 2.1.3），CLI `novita-sandbox-cli`；2.x 为 Novita 自有 API 形态（1.x 为 E2B 兼容的遗留版） | 直接用 2.x SDK；E2B 兼容仅作参考 | <https://docs.novita.ai/guides/sandbox-sdk-and-cli>、<https://docs.novita.ai/guides/sandbox-legacy-e2b-compatible> |
| 区域：`us-phx-1`（v2，SDK 默认，含 Secrets 与 Snapshots）、`us-virginia-1`（v1） | 默认 us-phx-1；与控制面的网络延迟需实测 | <https://docs.novita.ai/guides/sandbox-overview> |
| 生命周期：Running / Paused / Killed；`timeout` 为最长寿命，默认 5 分钟 | 必须显式设 timeout | <https://docs.novita.ai/guides/sandbox-auto-resume> |
| 命令执行支持后台与流式回调；文件读写、目录监听；每沙箱 20 GB | 满足 SPI 必需操作 | <https://docs.novita.ai/guides/sandbox-commands>、<https://docs.novita.ai/guides/sandbox-filesystem> |
| 网络：`allow_internet_access=False` 断网；`network.allow_out/deny_out` 支持 IP、CIDR、通配域名 | 网络四模式可原生映射 | <https://docs.novita.ai/guides/sandbox-internet-access> |
| 模板：从 Docker 镜像构建，CPU/内存按模板设定；有 `claude-code`、`codex`、`browser-chromium` 等预置模板；桌面沙箱支持 VNC 流 | 模板注册表；`in-sandbox` 原生外壳可行 | <https://docs.novita.ai/guides/sandbox-template-build-operations>、<https://docs.novita.ai/guides/sandbox-computer-use> |
| 配额（文档）：付费档并发 100、单会话最长 3 h、最大 8 vCPU / 8 GiB、磁盘 20 GB；产品页写 24 h【矛盾，待核实】 | 重型题路由约束 | <https://docs.novita.ai/guides/sandbox-quota-limit>、<https://novita.ai/sandbox> |
| 价格：$0.0000098/vCPU·s，$0.0000032/GiB·s（2 vCPU / 4 GiB 一小时约 $0.12） | 成本模型 | <https://docs.novita.ai/guides/sandbox-pricing> |
| 未提供 GPU 沙箱；隔离实现（是否 microVM）官方未说明【待核实】 | GPU 题不路由到 Novita；隔离画像实测 | <https://novita.ai/sandbox> |

### 8.3 macOS / Mac mini

| 事实 | 来源 |
| --- | --- |
| macOS EULA 允许每台 Apple 电脑额外运行最多 2 个 macOS 虚拟实例；Virtualization.framework 对 macOS guest 技术上限 2，对 Linux guest 无固定上限 | <https://developer.apple.com/forums/thread/845455> |
| Tart 已转到 `github.com/openai/tart`，许可 FSL-1.1-ALv2；`tart exec` 通过 Guest Agent 执行（需 macOS 14+）；`--net-softnet-allow/block` 只支持 CIDR | <https://github.com/openai/tart> |
| apple/container：每容器一个轻量 Linux VM，要求 macOS 26 + Apple Silicon；网络只有开/仅主机，无域名允许列表 | <https://github.com/apple/container> |

### 8.4 Windows

| 事实 | 来源 |
| --- | --- |
| Windows Sandbox 同时只能一个实例；Windows 11 24H2 起有 `wsb` CLI，但 `exec` 不回传进程输出、只返回退出码 | <https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli> |
| Hyper-V：PowerShell Direct 可在无网络时进入 VM 执行与拷文件；检查点可用于恢复 | <https://learn.microsoft.com/en-us/virtualization/hyper-v-on-windows/user-guide/powershell-direct> |

### 8.5 Harbor

| 事实 | 来源 |
| --- | --- |
| `BaseEnvironment` 必需方法为 start / stop / exec / upload_file / upload_dir / download_file / download_dir；无快照方法；`TaskOS` 只有 linux / windows | <https://github.com/harbor-framework/harbor/blob/main/src/harbor/environments/base.py> |
| 自定义环境可经 `-e module:Class` 注册；能力不满足时在校验阶段拒绝而非降级 | <https://docs.harborframework.com/sandboxes/custom-sandboxes.md>、<https://docs.harborframework.com/tasks/network-policies.md> |

---

## 9. 验收标准（系统级）

| # | 验收项 | 通过条件 |
| --- | --- | --- |
| AC-01 | HF 公开 / Gated / Private 三类模型各 1 个经 `novita-dedicated` 部署（2026-11-09：Novita 已停用，部署路径改由 `beam-pod` 等承担，见 PLAN.md）、探测、跑完一个小批次（3 题 × 1 epoch） | 三个 cell 均出分；`trial.versions` 中 HF revision、部署 ID、探测报告齐全 |
| AC-02 | 一个 OpenAI 兼容官方 API 模型与一个自部署模型同批 | 网关计费与预算强制生效；榜单标注口径 |
| AC-03 | 同一 Linux 题在 `docker` 与 `novita-sandbox` 上（2026-11-09：Novita 已停用，第二个真实载体待定，见 PLAN.md） | 参考解全过、空 Agent 全失败、金丝雀零跨 trial 命中 |
| AC-04 | macOS 题在 Mac mini（Tart）上 2 路并发稳定 24 h | 无槽位泄漏；宿主断电/断网演练后 trial 正确作废重排 |
| AC-05 | Windows 题在 Hyper-V 上跑通 | 流式输出、文件往返、用完销毁、预检全绿 |
| AC-06 | conformance kit | 所有已上线适配器 100% 通过其能力范围内用例 |
| AC-07 | 新增供应商演练 | 由未参与核心开发的工程师在 ≤ 5 人日接入一个新沙箱供应商（如 E2B 托管或 Daytona），核心零改动 |
| AC-08 | Harbor 桥接 | 用 `harbor run -e evalspleef.harbor_bridge:Env --ek provider=novita-sandbox` 跑通一个 Harbor 任务 |
| AC-09 | 继承 v2 闸门 | v2 的 P0/P1/P2 闸门（档位重判一致率 ≥95%、裁判标定 κ≥0.70 等，见附录 C.5）照旧 |
| AC-10 | 成本对账 | 一次完整批次的 token 费用、Novita GPU 账单、Novita 沙箱账单与事件日志对账误差 < 5% |

---

## 10. 里程碑（概要；详细 WBS 见 TECH-DESIGN §17）

| 里程碑 | 周 | 交付 | 闸门 |
| --- | --- | --- | --- |
| M0 最小闭环 | 1–4 | 两套 SPI、`openai-compatible`、`novita-dedicated`（assisted）、`docker`、`novita-sandbox`、网关（含 per-trial 密钥与预算）、能力探测、统一外壳 `remote-tools`、logic/data/debug 三类题跑通 | AC-01（至少公开模型）、AC-03 |
| M1 生产化核心 | 5–8 | hostd、`tart`（Mac mini）、conformance kit、`in-sandbox` 原生外壳、协议转换、成本对账、Gated/Private 冻结镜像流程、事件与审计扩展 | AC-01 全部、AC-02、AC-04、AC-06 |
| M2 扩展载体 | 9–11 | `hyperv`、`e2b-selfhost`（VPC）、`harbor-bridge`、`novita-serverless-gpu`、首个正式批次 | AC-05、AC-08、AC-10、NFR-04 |
| M3 冷门与常态化 | 12–14 | `libvirt`、`apple-container`、设备池/性能机接入 SPI、新增供应商演练 | AC-07、v2 P2 闸门 |

> 人力假设：3 名工程师（平台 ×2、模型接入/运维 ×1）+ 出题与标定人力另计。按实际团队规模校准。

---

## 11. 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| Novita DE 无管理 API，人工操作成为瓶颈与出错点 | 端点就绪慢、配置漂移 | assisted 模式把人工收敛到"控制台点几下 + 贴回两项"；系统做校验与探测；向 Novita 申请 API（Q1）；需要全自动时用 MP-3（`novita-serverless-gpu`） |
| DE 自动匹配引擎，工具调用解析器可能不适配某些开源模型 | 工具调用失败 → 看起来像"模型能力差" | 能力探测前置；探测失败的模型只跑逻辑赛道或改走 MP-3 指定 `--tool-call-parser`；运行期解析失败归 provider 类并复核 |
| 权重修订漂移（DE 重拉到新 commit） | 不可复现 | 冻结镜像仓库 + 批次前后 sha 复核 |
| 冷启动与空转计费 | 批次延迟或费用浪费 | 批次前预热、批次后缩零；空转告警 |
| Novita Sandbox 配额（3 h、8 vCPU/8 GiB）不够重型题 | 题目无法路由 | 能力匹配自动路由到本地/VPC；与 Novita 申请企业配额 |
| Novita Sandbox 隔离实现未公开 | 隔离强度申报不实 | 实测隔离画像；未证实前不承载 L3 要求的题 |
| macOS 2 VM/宿主硬上限 | 冷门题排期长 | 多台 Mac mini 横向扩；错峰；优先调度 |
| Windows 许可与镜像维护成本 | 进度风险 | P1 只做 1 个 Windows 题包验证；许可提前确认 |
| 协议转换（Anthropic/Responses ↔ Chat）丢失语义 | 原生外壳在开源模型上表现失真 | 探测 + 白名单：未通过转换探测的外壳 × 模型组合不进榜；榜单注明"经协议转换" |
| 适配器数量多，维护成本高 | 长期负担 | conformance kit + 版本锁定 + 只维护 P0/P1 适配器，P2/P3 按需 |
| Harbor 升级打断桥接 | 返工 | 桥接只依赖 `BaseEnvironment` 公开接口，锁版本，CI 跑桥接冒烟 |

---

## 12. 待决策事项（含推荐）

| # | 问题 | 推荐 |
| --- | --- | --- |
| Q1 | Novita Dedicated Endpoint 的管理是否接受"assisted"（人工在控制台创建 + 系统校验）？ | 接受作为 M0–M1 方案；并行向 Novita 商务/技术支持确认是否有未公开或企业版部署 API；**不做**控制台浏览器自动化 |
| Q2 | 需要定制引擎参数时，是否允许使用 Novita Serverless GPU（MP-3）作为同一模型的部署形态？ | 允许，但同一批次内同一模型只能有一种部署形态；部署形态写入 versions，跨形态结果标注不可直接比较 |
| Q3 | 自部署模型的榜单费用口径：参考单价（ref-price）还是实际 GPU 分摊？ | 榜单用 ref-price（可与 API 模型比较），成本报告同时给实际 GPU 分摊；两者都标注 |
| Q4 | 自部署开源模型是否跑原生外壳（经协议转换）？ | 首期只跑统一外壳；原生外壳 × 开源模型作为对照实验，不进主榜 |
| Q5 | 首期 Windows 题的范围？ | 仅 1–2 题（如 PowerShell/.NET CLI 工程题）验证载体；GUI 自动化题延后 |
| Q6 | Novita Sandbox 与本地 Docker 谁是 Linux 题的默认载体？ | 正式批次默认 Novita Sandbox（弹性并发，按秒计费），本地 Docker 用于开发自测与 Novita 配额外的重型题 |
| Q7 | 控制面部署位置？ | M0 在 Mac mini 上用 docker compose；M2 起迁到云上（与 Novita us-phx-1 网络就近），Mac mini 只跑 hostd |
| Q8 | 是否保留 Inspect AI 跑逻辑赛道？ | 保留（逻辑题不依赖沙箱），其模型调用改为经网关；Inspect 作为一个"Runner 适配器" |

---

## 附录（继承自 v2 规格的契约原文）

> **说明**：为使本文自包含，以下内容从 v2 规格（2026-11-06）原文照录，未改动语义。仅做了三类编辑性处理：
> 1. 指向其他文档的链接改为纯文本；
> 2. 指向已照录内容的引用改写为〔附录 X〕；
> 3. 文中的 `§` 编号保留原规格的章节号，仅作出处标记。
>
> 其中提到 Harbor 的配置（如 `environment_mode`、`[agent] user`、`harbor lint`）在 v3 中只在使用 Harbor 适配器时适用；v3 用各载体的隔离画像与预检来实现同等约束（TECH-DESIGN §7.7）。
>
> 枚举、状态、档位、预算字段名以本附录为准，v3 不做修改。

## 附录 A · 约定与共享枚举

### A.1 标识符


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

### A.2 共享枚举与预算字段


以下枚举拼写是硬约定，任何文档/Schema/代码必须逐字一致。

##### 4.1 Trial 状态（`trial.state`）

`pending` → `leased` → `running` → `observing` →（`awaiting_feedback` → `running`）* → `verifying` → `scored`；异常分支：`invalid`（基础设施/隔离问题作废）、`paused_manual`（人工介入）、`failed_infra`（重试后仍失败）。

- 合法转移表与失败处置写在 〔附录 B〕；Schema 只约束枚举。
- 重判（regrade）不新建 trial，而是产生新的 `grade` 版本，记录在 `trial.grades[]`。

##### 4.2 反馈级别（`feedback.level`）

`F0`（无反馈）/ `F1`（现象描述）/ `F2`（日志、报错原文）/ `F3`（视觉描述、协助操作）。语义、升级规则、泄露检查见 〔附录 C〕。

##### 4.3 档位（`grade.tier`）

枚举按优劣排序：`P`（Perfect）> `A` > `B+` > `B` > `C+` > `C` > `D` > `F`（Failed）。
榜单展示：`Perfect / A / B+ / B / C+ / C / D / Failed`；聚合器用的数值映射：`P=7, A=6, B+=5, B=4, C+=3, C=2, D=1, F=0`。
档位判定规则（含半档条件、F0 特例、Failed 条件）见 〔附录 C〕，本文只固定拼写与排序。

##### 4.4 缺陷严重度与扣分权重

`minor`=1 分 / `functional`=3 分 / `fatal`=5 分。扣分按"每轮新发现缺陷计入一次"累计，未修复不重复扣。展示格式 `{扣分}/{档位}({费用,2位小数})`，如 `6/A(290.08)`。

##### 4.5 检查类型（`check.kind`）

`script`（L1 硬门槛）/ `llm_judge`（L2 单条 rubric 判分）/ `agent_judge`（L2 能操作环境的裁判 Agent）/ `human`（L3）。

##### 4.6 错误分类（`error.class`）

`provider` / `model` / `cheat` / `isolation` / `environment` / `verification` / `judge` / `version`。
处置（`error.disposition`）：`retry`（成绩无效重试）/ `score`（成绩有效照常判分）/ `zero`（判 0 并标记）/ `manual`（转人工）/ `discard`（作废重跑）。

##### 4.7 成绩有效性（`trial.validity`）

`valid` / `invalid` / `pending` / `manual`。

##### 4.8 预算字段（task.yaml → trial，字段名冻结）

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

题目自定义值写入 \`task.yaml.budget\`，但不得超过批次硬上限；task 元数据 Schema 必须包含以上字段。

## 附录 B · 评测流

### B.1 批次级流程


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

### B.2 Trial 状态图与合法转移表

##### 4.1 状态

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

##### 4.2 合法转移表

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

### B.2a 租约与沙箱准备


| 步骤 | 动作 | 失败处置 |
| --- | --- | --- |
| S1 | 按平台申请资源：Linux 容器 / macOS VM 槽位 / 鸿蒙设备 / GPU 节点 | 排队；超时 30 min 告警 |
| S2 | 从固定 digest 镜像创建沙箱（预热池实例必须从未跑过 trial） | 重试 2 次，换宿主 |
| S3 | 生成 trial 金丝雀令牌并埋点（见 〔附录 D〕） | 失败即 invalid |
| S4 | 身份去标识：随机工作区路径、中性用户名、清空 `$HOME` 模板 | 失败即 invalid |
| S5 | 隔离预检 I1–I9（见 〔附录 D〕 第 5 节） | 任一失败 → 换宿主重试 |
| S6 | 注入任务资源（非隐藏部分），启动外壳 | 失败按环境错误处理 |

> **不依赖 Harbor 默认值**：`environment_mode=separate`（老任务默认 shared）、`[agent] user`（非 root 是 opt-in）、`network_mode`、lint 的 `unpinned-image`/`root-agent` 规则（默认关闭，需 `--extend-select`）都必须在题包中显式声明并由预检断言。详见 〔附录 D〕 与〔TECH-DESIGN §9〕。

### B.3 运行轮与停止原因


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

### B.4 停止条件与优先级


终态判定按以下优先级短路，任何时刻只取第一条：

| 优先级 | 条件 | 终态 | 成绩 |
| --- | --- | --- | --- |
| P0 | 基础设施/隔离失效（金丝雀跨 trial、预检失败、宿主污染） | invalid | 无效，重跑 |
| P1 | 作弊命中（改测试、写 reward、硬编码、查答案、绕过网络、注入裁判） | verifying→scored | 有效，记 0 + 标记 |
| P2 | 硬门槛全过且无未修复 functional/fatal 缺陷 | verifying→scored | 正常判档（P/A） |
| P3 | 预算耗尽（轮数/墙钟/token/费用） | verifying→scored | Failed |
| P4 | 不可继续（反馈无法生成、Agent 连续崩溃但非环境问题） | verifying→scored | Failed |
| P5 | 人工中断 30 min 未恢复 | invalid | 无效，重跑 |

### B.5 预算模型


##### 7.1 预算字段（trial 级，来自 task.yaml）

| 字段 | 默认值（标准题） | 重型题默认 | 说明 |
| --- | --- | --- | --- |
| `max_rounds` | 6 | 10 | 含首轮；反馈轮数 = max_rounds-1 |
| `max_feedback_rounds` | 3 | 5 | 实际发出反馈的轮数上限（F0 催促不计入） |
| `max_nudges` | 2 | 2 | 无信息量催促次数上限，计 F0、不降档 |
| `max_total_tokens` | 2,000,000 | 8,000,000 | 被测 Agent 的模型输入+输出总量 |
| `max_wall_clock_s` | 3600 | 10800 | 从 running 开始计时，不含排队与 teardown |
| `max_cost_usd` | 15 | 80 | 被测 Agent 侧费用上限（裁判/用户费用单列） |
| `max_observe_s` | 600 | 1200 | 单轮观察超时 |

字段名已冻结（见〔附录 A.2〕），task 元数据 Schema 必须包含。`escalation.f1_no_progress_rounds` / `escalation.f2_no_progress_rounds` 默认 2 轮。

冷门平台题默认上浮 1.5 倍（macOS/鸿蒙/性能机）；题包可在 `budget` 中显式覆盖，但不得超过批次硬上限。

##### 7.2 强制点

| 预算 | 强制点 | 超限行为 |
| --- | --- | --- |
| cost | Gateway 实时累计，每请求前检查 | 拒绝请求，停止原因 `cost_cap` |
| token | 外壳每步上报，Gateway 双校验 | 同上 |
| wall clock | 控制面计时器 | 杀进程组，停止原因 `timeout` |
| rounds | Controller 决策时检查 | 直接 verifying，终态 Failed |
| nudges | Controller 决策时检查 `max_nudges` | 不再发无信息量催促，转 F1 或终态 |
| 批次总预算 | Planner 冻结时锁定，Scheduler 每派发前检查 | 停止派发，告警 |

##### 7.3 费用口径

```
trial.cost.agent = Σ_round (input_tokens × price_in + output_tokens × price_out + cache_read × price_cache)
trial.cost.judge = Σ_judge_call (裁判模型费用)
trial.cost.user  = Σ_feedback (模拟用户模型费用)
trial.cost.total = agent + judge + user
榜单单元格费用 = median(各 epoch 的 cost.agent)   # 与参考项目口径一致
批次成本 = Σ trial.cost.total + 基础设施成本（另表）
```

价格表按官方价锁定版本，写入 batch 记录；不得使用会变动的实时价。

### B.6 异常、重试、重判、作废


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

### B.7 Cell 状态与榜单归属


| cell 状态 | 含义 | 是否进排名 |
| --- | --- | --- |
| Pending | 未开始 | 否 |
| Running | 至少一个 trial 在跑 | 否 |
| Scored | 所有 epoch 有有效成绩 | **是** |
| Pass | 人工标记"前代已 A，跳过" | 是（标注继承自哪个前代模型） |
| Skip | 人工标注原因跳过（不适用/题目退役） | 否（榜单显示"未测"） |
| Manual | 进入人工仲裁 | 暂不进 |

聚合规则：cell 得分 = 各 epoch 的**中位档位**；展示最好/最差档位；档位跨度 ≥2 档进人工复核。费用 = 中位 agent 费用。

### B.8 事件日志契约


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

## 附录 C · 判定

### C.1 三层判分与硬门槛短路

##### 2.1 总表

| 层 | 判什么 | 输入 | 输出 | 失败语义 | 谁能改结论 |
| --- | --- | --- | --- | --- | --- |
| L1 脚本（硬门槛） | 能构建、能启动、隐藏测试通过、产物存在且格式正确、数值在容差内、性能达标、设备上功能可用 | 验证沙箱里的产物 + 隐藏测试 + 观察器采集的截图/日志/帧时间 | check[]：每项 {check_id, kind:"script", passed, evidence} | 任一项 fail ⇒ **本次 trial 不能判成功**；是否继续由反馈控制器决定（预算内继续修，预算尽判 Failed） | 不可被 L2 改判；人工可判 invalid（基础设施问题）但不能把 fail 改 pass |
| L2 裁判 | 视觉是否符合描述、交互是否顺手、科学细节是否讲对、代码结构是否合理、推导是否成立 | 产物快照 + 截图/视频帧 + 日志 + rubric 条目（+ 逻辑题参考推导） | 每条 rubric 一个 pass/fail/inconclusive + 证据；失败条目转成缺陷记录（含严重度） | 裁判超时/输出非法/一致性低 ⇒ 该条 inconclusive ⇒ **转人工**，绝不静默记 0（〔附录 B〕 §8） | 人工仲裁可覆盖；重判生成新 grade 版本，不覆盖旧版本 |
| L3 人工 | 新题上线前标定、仲裁队列、Perfect/Failed 边界档、随机抽检 | trial 轨迹、证据、rubric、机器判定 | 人工标签 tier + 理由；覆盖时写新 grade 版本 | 人工判定优先于机器；但必须留痕（decided_by:human/arbiter + manual_notes） | 人工是最终权威；覆盖必须能解释「为什么规则错了」，并回写 rubric 改进项 |

##### 2.2 L1 硬门槛的判定口径

硬门槛必须是**可脚本复现**的检查，且只使用验证侧资源（隐藏测试与隐藏数据不进 Agent 镜像，见 答案与验收规范）。允许的硬门槛类别：

| 类别 | 示例 | 通过口径 |
| --- | --- | --- |
| build | cargo build、cargo test、xcodebuild test、hvigorw assembleHap、vulkan 验证层零报错 | 退出码 0 且无错误级日志 |
| run | 服务启动、页面可加载、App 可安装启动 | 进程存活/端口监听/截图非空 |
| test | 隐藏测试套件 | 退出码 0；测试文件哈希未被修改（防改测试） |
| artifact | 产物存在、格式、时长、分辨率、编码 | ffprobe/schema 校验/文件哈希 |
| numeric | 数值与解析解/高精度参考值比较 | 按题目声明的 abs/rel 容差（§12.2） |
| performance | 帧时间、FPS、内存峰值 | 设备/性能机上跑 5 次取中位数，阈值留余量；性能机抖动按 〔附录 B〕 §8 处置 |
| device | 鸿蒙/iOS 设备上的功能检查 | 设备池脚本退出码 0 + 截图/hdc 日志 |

硬门槛结果只有 passed: true/false 两态；**没有部分通过**。若某题确实存在「可选加分项」，它属于 L2 软 rubric，不属于硬门槛。

##### 2.3 L2 裁判的判定口径

1. 每条 rubric 只输出 pass / fail / inconclusive 三态；inconclusive 只能出现在证据不足或裁判故障时，且必须进人工队列，不能当 fail 用。
2. 判 fail 必须附证据（截图、日志片段、文件路径等）；证据缺失时裁判必须输出 inconclusive。
3. 评审团至少两家不同厂商的模型；裁判模型家族必须与被测模型不同家族；同一 trial 内裁判上下文中不得出现别的 trial 的产物（〔附录 D〕 第 5 节）。
4. 每条 rubric 采样 k=5，取中位结论；一致率低于阈值触发仲裁（§11.2）。
5. 裁判不得因为产物里写了指令而改变评分；命中「写给裁判的指令」按作弊处理（§11.7）。

##### 2.4 L3 人工的判定口径

人工只在四类场景出现，**不做全量打分**（目标人工工时 ≤ 全量 10%，基线方案口径）：新题/新 rubric 上线前的标定；仲裁队列；Perfect 与 Failed 边界；每期随机抽检（默认 5%，不进 P1 目标）。人工覆盖档位时必须记录：原机器档位、覆盖后档位、理由、复核人、时间；并生成新的 grade 版本（supersedes 指向旧版本）。

##### 2.5 硬门槛短路规则

判定顺序是**先排除、后聚合**。任一短路命中即停止后续规则，且不允许 L2 或 L3 在没有留痕的情况下改判：

| 编号 | 条件 | 处置 | 是否产出正式 grade |
| --- | --- | --- | --- |
| R0 | trial.validity=invalid（基础设施/隔离失效） | 作废重跑，不参与榜单 | 否（G-INVALID-00 只记事件） |
| R1 | cheat 信号命中且人工复核确认（或 harbor analyze/哈希锁确证） | 判 0、标 cheat，有效成绩进榜（G-CHEAT-01） | 是 |
| R2 | validity=manual（裁判不可用/一致性低/人工介入） | 转人工，人工给出档位后写 grade | 人工出 |
| R3 | 硬门槛任一 fail 且预算耗尽 | Failed（G-BUDGET-F） | 是 |
| R4 | 硬门槛任一 fail 且预算未耗尽 | **不终判**，反馈控制器继续（§7） | 否（继续跑） |
| R5 | 硬门槛全过且无未修复 functional/fatal 缺陷 | 按 §9 聚合档位（P/A/B/B+/C/C+/D） | 是 |
| R6 | 硬门槛全过但存在未修复 functional/fatal 缺陷且预算耗尽 | Failed（G-BUDGET-F，flags 含 budget_exhausted） | 是 |
| R7 | 反馈无法生成（3 次泄露重写失败）或「不可继续」 | Failed（G-ABORT-04） | 是 |
| R8 | 逻辑题格式违规命中（要求不解释却写解释、要求不写代码却给代码） | 判 0（G-FORMAT-05，F + format_violation，penalty=0），与参考项目口径一致 | 是 |

> R5 中的「无未修复 functional/fatal 缺陷」是成功条件；只剩 minor 缺陷时仍算通过（最高 A），零缺陷才是 P。该口径与 〔附录 B〕 §5.4 / §6 一致。

---

### C.2 反馈阶梯（级别语义、字段白名单、目标选择、升级规则）

##### 7.2 级别语义

| 级别 | 允许说的内容 | 禁止内容 | 典型话术 |
| --- | --- | --- | --- |
| F0 | 无信息量：初始任务说明、「我试用了一下」、「请按你的判断做完」、按 FAQ 回答澄清 | 任何具体问题、任何缺陷线索 | 见 persona 话术模板 F0 段 |
| F1 | 普通用户视角的现象：操作路径 + 看到什么 + 期望什么 | 文件名、函数名、API 名、日志原文、根因、修复建议、代码 | 「我点旋转后，模型就不见了；我期望它继续转。」 |
| F2 | F1 内容 + 日志/报错原文（预写、脱敏、逐字粘贴） | 根因、修复建议、代码、隐藏测试信息 | 「控制台里有这么一段：…（原文）」 |
| F3 | F2 内容 + 视觉差异描述 + 协助操作（签名/装机/点击等） | 代码、补丁、根因、修复步骤 | 「截图里模型只剩一个角；我来帮你点一下签名按钮。」 |

累计语义：F3 ⊇ F2 ⊇ F1 ⊇ F0（每级包含更低级允许的内容）。对单个缺陷，本轮最多披露到「该缺陷当前被允许级别」；整轮消息级别 = 目标缺陷所需级别。

##### 7.3 字段白名单矩阵

| 缺陷字段 | F0 | F1 | F2 | F3 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 现象/操作路径（f1_symptom） | 禁 | 允 | 允 | 允 | 用户可见事实 |
| 日志/报错原文（f2_evidence） | 禁 | 禁 | 允 | 允 | 必须是预写片段，逐字粘贴，不得改写为提示 |
| 视觉差异（f3_visual） | 禁 | 禁 | 禁 | 允 | 只描述看到的差异，不解释原因 |
| 协助操作（f3_assist） | 禁 | 禁 | 禁 | 允 | 仅限 Agent 无法自行完成的动作 |
| hidden 条目的任何素材 | 禁 | 仅 f1_symptom | 禁 | 禁 | 隐藏验收不得通过 F2/F3 泄露 |
| 参考答案/隐藏测试/rubric 编号/严重度 | 禁 | 禁 | 禁 | 禁 | 任何级别都禁止（§8） |

##### 7.4 目标缺陷选择

每轮只选一个目标，顺序：

1. 只考虑未修复且 severity ∈ {functional, fatal} 的缺陷（minor 永不选）。
2. 按 severity 降序（fatal > functional）。
3. 同严重度按 first_round 升序（先发现先反馈，避免饿死）。
4. 仍并列按 defect_id 字典序（确定性）。
5. 若所有未修复缺陷都是 minor ⇒ 不再反馈（成功条件只看阻断缺陷）。

##### 7.5 升级规则（精确计数）

对目标缺陷 d，设其反馈历史为 h（按轮次升序）：

- last_level(d) = h 中最后一条反馈的级别；无历史时为 F1。
- 若 last_level = F1：令 n1 = 从最后一条开始向前数、连续「级别=F1 且 progress=none」的轮数（遇到 F2/F3 或 progress∈{partial,fixed} 停止）。若 n1 ≥ escalation.f1_no_progress_rounds（默认 2）⇒ 本轮用 F2；否则 F1。
- 若 last_level = F2：同理令 n2；若 n2 ≥ escalation.f2_no_progress_rounds（默认 2）⇒ F3；否则 F2。
- 若 last_level = F3：维持 F3，直到修复或预算耗尽。

注意三点：

1. **只有「连续」才累计**：中间出现 partial 进展会把计数清零（见 §6.4），避免升级过慢或过快。
2. **升级按缺陷独立计数**：A 缺陷在 F2 不影响 B 缺陷从 F1 开始。
3. **F2/F3 一旦发出，trial 的 max_level 即被抬高**，最终档位最高 D（§9.3）——即使缺陷随后修好。这是「用了多少帮助」的直接度量。

### C.3 档位算法

##### 9.1 输入量定义

档位是**确定性函数**，输入只有 trial 级聚合量。先把它们定义清楚（实现见参考实现 grading 模块；测试向量 V01–V13）：

| 符号 | 定义 | 说明 |
| --- | --- | --- |
| hard_gates_passed | 全部 L1 检查 passed=true | 布尔 |
| validity | valid/invalid/pending/manual | 〔附录 A〕 §4.7 |
| cheat_flag | 作弊检测命中（哈希锁/analyze/guard/网络审计） | 布尔 |
| format_violation | 逻辑题格式违规命中（§12.5） | 布尔 |
| total_defects | 累计去重缺陷记录数（含已修复与回归新记录） | 整数 |
| open_blocking | 终判时 fixed_round 为空且 severity∈{functional,fatal} 的缺陷数 | 整数 |
| success | hard_gates_passed 且 open_blocking=0 | 布尔 |
| max_level | 已发出的实质反馈（F1-F3）中的最高级别；未发过 = F0 | F0/F1/F2/F3 |
| feedback_rounds | 已发出的实质反馈轮数（F1-F3，不含 F0 催促；对应 grade.feedback_summary.feedback_rounds） | 整数 |
| multi_round_f1 | 满足 max_level_during_open=F1 且 feedback_rounds≥2 的已修复缺陷数 | 整数 |
| need_feedback | 满足 feedback_rounds≥1 且已修复的缺陷数（半档分子） | 整数 |
| half_step | 整数比较 3 × need_feedback ≤ total_defects 成立 | 布尔 |
| budget_exhausted | 轮数/token/墙钟/费用任一触底 | 布尔 |
| feedback_blocked | 反馈无法生成（3 次泄露重写失败）或「不可继续」 | 布尔 |

> 关键口径：max_level 只看**实际发出的反馈**；F0 催促与澄清问答不算。need_feedback 与 multi_round_f1 都按**缺陷记录**计数（回归按新记录算）。

##### 9.2 判定优先级与规则表

按优先级从 0 开始逐条匹配，**命中第一条即停止**；不允许同时命中两条。P0/P14 的「无 grade」表示只写事件与 trial 状态，不写自动 grade；P3 由人工产出 grade（decided_by=human/arbiter）。

| 优先级 | rule_id | 条件（全部满足） | tier | 关键 flags | grade |
| --- | --- | --- | --- | --- | --- |
| P0 | G-INVALID-00 | validity=invalid | — | invalid | 不产出 |
| P1 | G-CHEAT-01 | cheat_flag=true | F | cheat, zeroed | 产出（penalty=0） |
| P2 | G-FORMAT-05 | format_violation=true | F | format_violation, zeroed | 产出（penalty=0） |
| P3 | G-HUMAN-99 | validity=manual | — | manual_arbitration | 人工产出 |
| P4 | G-F0-PERFECT | success 且 max_level=F0 且 total_defects=0 | P | — | 产出 |
| P5 | G-F0-MINOR | success 且 max_level=F0 且 total_defects>0 且全部缺陷为 minor | A | minor_only | 产出 |
| P6 | G-F1-BPLUS | success 且 max_level=F1 且 multi_round_f1=0 且 half_step | B+ | half_step | 产出 |
| P7 | G-F1-CPLUS | success 且 max_level=F1 且 multi_round_f1>0 且 half_step | C+ | f1_multiround, half_step | 产出 |
| P8 | G-F1-B | success 且 max_level=F1 且 multi_round_f1=0 且 not half_step | B | — | 产出 |
| P9 | G-F1-C | success 且 max_level=F1 且 multi_round_f1>0 且 not half_step | C | f1_multiround | 产出 |
| P10 | G-F2-D | success 且 max_level=F2 | D | — | 产出 |
| P11 | G-F3-D | success 且 max_level=F3 | D | — | 产出 |
| P12 | G-BUDGET-F | not success 且 budget_exhausted | F | budget_exhausted（硬门槛未过再加 hard_gate_failed） | 产出 |
| P13 | G-ABORT-04 | not success 且 feedback_blocked 或已判「不可继续」 | F | feedback_blocked 或 terminated_early | 产出 |
| P14 | G-ANOMALY-98 | not success 且未预算耗尽、未 abort（数据异常） | — | terminated_early | 不产出，转人工 |

> 注意 P4-P11 全部要求 success=true（即硬门槛全过且无未修复 functional/fatal）。**D 只在最终通过时成立**：用到 F2/F3 但最终失败 ⇒ P12/P13 判 F，而不是 D。

##### 9.3 max_level 与大档映射

| max_level | 成功时可能档位 | 规则 |
| --- | --- | --- |
| F0 | P / A | 无实质反馈：零缺陷 P；只有 minor A |
| F1 | B+ / B / C+ / C | 单轮修复 B 族；同一缺陷多轮 F1 修复 C 族；半档条件满足升半档 |
| F2 | D | 用到日志级帮助，上限 D |
| F3 | D | 用到视觉/协助级帮助，上限 D |
| 任意 | F | 未通过（预算耗尽/不可继续/作弊/格式违规） |

半档只在 B/C 族内部生效（B→B+、C→C+）；不存在 A+ 或 D+。**档位不进位、不四舍五入**。

##### 9.4 半档规则（1/3 条件）

定义：

- 分子 need_feedback = 需要至少一轮反馈才修复的缺陷数（修复后按一条缺陷记录计）。
- 分母 total_defects = 本 trial 累计去重缺陷记录数（含只有 minor、从未反馈的缺陷，含回归新记录）。
- 条件：**3 × need_feedback ≤ total_defects**（整数比较，避免浮点误差）。

例子：

| 场景 | need_feedback | total_defects | 3×need ≤ total? | 单轮 F1 时档位 | 多轮 F1 时档位 |
| --- | --- | --- | --- | --- | --- |
| 1 个 functional 需反馈修好，无其它问题 | 1 | 1 | 3≤1 否 | B | C |
| 1 个 functional 需反馈修好 + 2 个 minor 未修 | 1 | 3 | 3≤3 是 | B+ | C+ |
| 2 个 functional 都需反馈、各一轮修好 | 2 | 2 | 6≤2 否 | B | C |
| 2 个 functional 需反馈 + 4 个 minor | 2 | 6 | 6≤6 是 | B+ | C+ |

> 该规则对应 plan-v1 §4.3「同档内若需要反馈的缺陷数不超过全部缺陷三分之一，升半档」；「三分之一」是**建议值**，已按 Lead 决策冻结为 1/3，后续若人工标定显示系统性偏移，改 rubric 与题包而不是改这个常数。

##### 9.5 F0 特例与 P/A 边界

| 场景 | 判定 |
| --- | --- |
| 零缺陷通过 | P。penalty 必须为 0（Schema 强制）。 |
| 只有 minor 缺陷（无论几轮观察、无论催促几次） | A。penalty = minor 数量。 |
| F0 催促用过（nudges_used ≤ max_nudges） | 不影响 P/A：催促是无信息量消息，不计反馈轮、不降档。 |
| 澄清问答用过 | 不影响档位；按 FAQ 回答不算帮助。 |
| 催促用尽后仍未完成 | 按预算/不可继续终判；若硬门槛全过且无阻断缺陷仍可 P/A。 |
| success 但 max_level=F0 且存在 blocking 缺陷记录 | 数据异常（违反 §9.8 I2），走 G-ANOMALY-98 转人工。 |

##### 9.6 Failed 条件（全部情形）

| 情形 | 规则 | penalty |
| --- | --- | --- |
| 预算耗尽且未满足成功条件 | G-BUDGET-F | 按已发现缺陷累计（§10.1） |
| 硬门槛全过但存在未修复 functional/fatal 且预算耗尽 | G-BUDGET-F | 同上 |
| 反馈无法生成（3 次泄露重写失败） | G-ABORT-04 | 同上 |
| Agent 连续崩溃但分类为模型问题、无法继续 | G-ABORT-04 | 同上 |
| 作弊 | G-CHEAT-01 | 0 |
| 逻辑题格式违规 | G-FORMAT-05 | 0 |
| 基础设施/隔离失效 | G-INVALID-00 | 无 grade（不计榜） |
| 数据异常（不应出现的状态） | G-ANOMALY-98 | 无 grade，转人工 |

### C.4 扣分累计规则

##### 10.1 扣分累计规则

| 规则 | 内容 |
| --- | --- |
| 权重 | minor=1 / functional=3 / fatal=5（〔附录 A〕 §4.4） |
| 计一次 | 每个缺陷记录在**首次发现轮**计一次权重；persisting 不重复计 |
| 回归 | 修复后再次失败 ⇒ 新缺陷记录，重新计一次（并计 regressed） |
| 去重 | 同一轮同一 rubric 条目只登记一条；证据合并 |
| 目录外 | U-序号 不计分，只进 rubric 改进队列 |
| 扣分上限 | 不设上限，但见 §10.5 的合理性检查；实际出现 >30 分说明题包 rubric 过密，应复核 |
| 结局无关 | 已修复不追回、未修复不减免；扣分记录的是「模型犯过多少错」，档位记录的是「需要多少帮助」 |
| 零分特例 | 作弊与格式违规 penalty=0（判 0 已到下限，标记优先于扣分） |

### C.5 裁判面板与一致性

##### 11.1 面板组成

| 角色 | 数量 | 约束 | 用途 |
| --- | --- | --- | --- |
| llm_judge | ≥2 个不同厂商模型 | 与被测模型不同家族；版本锁定；prompt 版本锁定 | 视觉、代码结构、科学细节、推导覆盖度 |
| agent_judge | 0-1 | 在只读 overlay 沙箱中运行，allowed_tools 白名单 | 需要自己点页面/跑命令的交互与功能检查 |
| arbiter（人工或第三方模型） | 0-1 | 仅在仲裁队列出现 | 分歧裁决，不参与常规全量 |

反同源规则：裁判模型家族 ≠ 被测模型家族；裁判之间尽量不同厂商（Harbor 文档支持用 LiteLLM 指定任意裁判模型与多采样；Inspect 支持多裁判模型多数投票）。评审团与采样的落地见〔TECH-DESIGN §10〕。依据：[Harbor Rewardkit judge criteria](https://docs.harborframework.com/rewardkit/judge-criteria.md)、[Inspect model grading](https://inspect.aisi.org.uk/model-graded.html)、PoLL 论文（多小模型评审团比单一巨模型偏差更小且更便宜，[arXiv:2404.18796](https://arxiv.org/abs/2404.18796)）。

##### 11.2 采样与聚合

1. 每条 rubric 每条裁判采样 k=5；裁判输出 pass/fail/inconclusive。
2. 聚合：pass=1、fail=0、inconclusive 不投票；中位数（二元即多数票）；平票且无多数 ⇒ inconclusive。
3. 一致率 agreement = 与中位结论一致的采样数 / 有效采样数。
4. 处置阈值：

| 条件 | 处置 |
| --- | --- |
| agreement ≥ 0.8 | 直接采用中位结论 |
| 0.6 ≤ agreement < 0.8 | 该裁判加采到 9 次；仍 <0.8 进人工 |
| agreement < 0.6 | 该条 inconclusive，进人工仲裁 |
| 不同裁判模型结论相反 | 进人工仲裁（记录 panel_split） |
| 采样超时/非法输出 | 重试；仍失败 ⇒ inconclusive ⇒ 人工（不静默记 0） |

> Harbor 的 samples 语义是「多次运行取每条 criterion 的中位数，并报告 agreement（0-1）」，与本文口径一致；其 on-error 策略 fail/drop 我们取「fail 转人工」而不是 drop 记 0（[Rewardkit judge criteria](https://docs.harborframework.com/rewardkit/judge-criteria.md)）。

##### 11.3 一致性指标与阈值

| 指标 | 适用 | 公式/工具 | 阈值（建议值） |
| --- | --- | --- | --- |
| 完全一致率 percent_agreement | 任意，直观 | 一致样本/总样本 | 标定闸门 ≥0.70；相邻档一致率 ≥0.90 |
| Cohen kappa | 两个评分者、类别型（档位/单条 pass-fail） | κ=(po−pe)/(1−pe) | ≥0.61 substantial、≥0.81 almost perfect（Landis & Koch 1977 经验带） |
| Krippendorff alpha | ≥3 评分者、有缺失、可处理序数距离 | α=1−Do/De | α≥0.800 可用；0.667≤α<0.800 仅限探索性结论；<0.667 不可接受（Krippendorff 的常用经验阈值，本方案取为建议值） |

依据：[Cohen 1960](https://doi.org/10.1177/001316446002000104)；[Landis & Koch 1977](https://doi.org/10.2307/2529310)；[Hayes & Krippendorff 2007](https://doi.org/10.1080/19312450709336664)。档位是有序类别，计算 kappa 时用序数权重（weighted kappa）更敏感；alpha 用 ordinal metric。

## 附录 D · 隔离

### D.1 隔离强度分级

##### 3.1 隔离强度分级

| 级别 | 定义 | 共享内核？ | 典型实现 | 用在本项目哪里 |
| --- | --- | --- | --- | --- |
| L0 | 进程内/同宿主同用户，无隔离 | 是 | 直接在本机跑 | **禁止** |
| L1 | 容器 + 只读根 + 非 root + 私有 tmpfs + 私有 netns + 资源配额 | 是（宿主内核） | docker/podman（runc） | 默认 Linux 工程题 |
| L2 | L1 + 用户态内核拦截系统调用 | 部分（Sentry 拦截，宿主 syscall 面极小） | gVisor（runsc）；Modal 默认 runtime | 需跑不可信二进制/依赖崩溃隔离的题 |
| L3 | 硬件虚拟化，独立 guest 内核 | 否 | Firecracker microVM、Tart macOS VM、托管 provider 的 VM 运行时 | 越界风险高的题、macOS/iOS 题、多租户托管 |
| L4 | 物理独占 | 完全物理隔离 | 鸿蒙真机、macOS 性能裸机、单租 GPU 卡 | 设备类题、性能/帧率类题 |

**选级规则**：默认 L1；题目允许执行任意下载二进制、需要内核模块/网络栈定制、或来源模型风险高时升 L2/L3；涉及真机/帧率时用 L4。不允许用 L1 承载 macOS/鸿蒙/性能类验收（平台矩阵以题库文档 〔附录 E.2〕 为准，该文由 tasks-a 维护）。

### D.2 隔离不变量 I1–I16

I1–I9 是**预检不变量**（〔附录 B.2a〕S5 的 "I1–I9 检查通过"）；I10–I16 是运行期/产后期不变量。每条都要在 `trial.isolation.checks[]` 留下一条 `result=pass` 记录（trial Schema 的 `isolation_check` 结构）。

##### 5.1 一览表

| # | 不变量 | 强制手段（实现） | 验收断言（可执行） | 违反处置 |
| --- | --- | --- | --- | --- |
| I1 | 一个 trial 独占一个沙箱，租约全局唯一且用完即毁 | 调度器原子租约；批次内 lease 唯一索引；销毁后不得回池 | `python3 -c 'import json,glob;r=[json.load(open(p)) for p in glob.glob("trials/*/trial.json")];L=[x["isolation"]["sandbox"]["lease_id"] for x in r];assert len(L)==len(set(L))'` 且每条 `destroyed_at` 非空 | 调度器 bug → 停批排查；涉事 trial invalid |
| I2 | 根文件系统只读 | 容器 `--read-only` / VM 只读根盘；可写处仅 tmpfs/overlay | 沙箱内 `findmnt -no OPTIONS / \| grep -q '^ro'` 且 `touch /.__probe` 失败 | 预检 fail → 换宿主重试 |
| I3 | `/tmp`、`$HOME`、工作区为 trial 私有（tmpfs/overlay），跨 trial 不可见 | 每 trial 新 tmpfs + 随机工作区；禁止宿主 bind mount | 双 trial 负向探测：A 写 `/tmp/marker-A`，B `test ! -e /tmp/marker-A`；`findmnt -no FSTYPE /tmp` 为 `tmpfs` | 同上 |
| I4 | 私有网络命名空间 + 默认断网 | 每 trial 独立 netns；`network_mode=no-network`；egress 由代理侧强制 | `readlink /proc/self/ns/net` 与宿主不同；`curl --max-time 3 https://example.com` 非 0 退出；`ip -o link` 无宿主网卡 | 同上；若 egress 通 → 安全事件 |
| I5 | 无宿主可写挂载、无 docker socket、无特权 | lint 规则 `compose-writable-bind-mount`/`compose-privileged`/`compose-additional-capabilities`；启动后复查挂载表 | `findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS` 中无 `rw` 宿主路径；`test ! -S /var/run/docker.sock`；`grep -q 'CapEff:.*0000003fffffffff'` 视为过宽 | 预检 fail；lint 规则 ignore 需 Lead 批准 |
| I6 | **必须显式配置非 root/受限用户**，capability 最小集、无 sudo；**不得依赖 Harbor 默认值**（Harbor 默认不创建 agent 用户，非 root 是 opt-in） | 题包模板强制 `[agent] user="agent"`；容器 drop all caps；无 setuid/sudo 可用 | `id -u` ≠ 0；`sudo -n true` 失败；`capsh --print \| grep -c 'cap_sys_admin'` = 0 | 预检 fail → 修镜像/配置；模板未写该字段直接判题包不合格 |
| I7 | CPU/内存/磁盘/进程数/墙钟配额 | Harbor `cpus/memory_mb/storage_mb` + cgroup v2；`pids.max`；控制面计时器 | `cat /sys/fs/cgroup/{cpu.max,memory.max,pids.max}` 与 task.yaml 资源一致；`nproc` ≤ 配额 | 配额缺失 → 预检 fail；OOM 按 〔附录 B〕 §8 判 |
| I8 | 身份去标识（中性名、随机工作区、无 eval 痕迹） | 随机工作区名；中性用户名/主机名；环境变量不含题目/模型/批次标识 | `pwd` 不匹配 `task_id/model/batch`；`env` 中无 `TASK_ID/MODEL/BATCH/EVAL/HARBOR` 变量；hostname 为中性名 | 预检 fail → 修 provisioner |
| I9 | 镜像 digest 固定 + 金丝雀注入完成 + 无凭证入沙箱 | 镜像按 digest 启动（lint `unpinned-image`）；令牌写入 `canary.md` 全部位置；密钥扫描 | `sha256sum` 每个埋点文件与 `isolation.canary.placements[].sha256` 一致；`env`/文件扫描无 `sk-`/私钥；`image_digest` 形如 `sha256:[a-f0-9]{64}` | 金丝雀注入失败/密钥入沙箱 → invalid + incident |
| I10 | 验证沙箱独立，隐藏测试只在验证侧 | Harbor `[verifier] environment_mode="separate"`；独立镜像/独立租约；Agent 镜像 lint `agent-image-test-leak` | `verifier_lease_id != agent_lease_id`；验证侧镜像中 `find / -name 'test_*.py' -o -name 'solution*'` 只出现在 `/tests`；Agent 镜像内无隐藏测试 | 违反 → 该题下线重做 |
| I11 | 产物按声明导出并净化（软链/FIFO/socket/设备/setuid/大小/数量） | Harbor artifact sanitize（等价 tar `data` filter）；白名单路径；大小/数量上限 | 产物清单中无 `type∈{symlink,fifo,socket,device}`、无 setuid 位；总体积 ≤ 声明上限；每个产物有 sha256 | 违反条目丢弃并记警告；夹带即 cheat 线索 |
| I12 | 测试哈希锁 + 验证前清空旧 reward | `tests.sha256` 清单；verifier 启动时删除 `reward.txt/reward.json`（Harbor 已默认做）；隐藏测试只读挂载 | 验证开始前 reward 不存在；测试文件哈希与清单一致；不一致 → cheat | 改测试 → `G-CHEAT-01` 判 F/0；重写 reward → 同上 |
| I13 | 金丝雀跨 trial 扫描零 critical 命中 | 每 trial 唯一令牌；批次结束交叉扫描产物/轨迹/网络日志；命中即作废双方 | `python3 -c 'import json;d=json.load(open("canary/scan.json"));assert d["critical"]==[]'`；`trial.isolation.canary.scan_status ∈ {clean, not_scanned}` | critical 命中 → 双方 invalid + incident（§8） |
| I14 | 设备/宿主归还卫生，跑过 trial 的实例不回池 | 独占租约；归还检查清单（§11）；基线哈希；预热池只收新实例 | `handover_check.status=pass` 且 `package_diff.added/removed` 为空；`baseline_hash` 与基线指纹一致；`from_warm_pool=true` 的实例无 trial 历史 | 归还 fail → 设备摘除 + incident；实例销毁不回池 |
| I15 | 裁判/模拟用户上下文按 trial 隔离 | 每 trial 独立裁判会话；模拟用户只拿白名单字段；裁判输入快照留档；不引用他 trial 文本 | 裁判会话 ID 唯一；`judge/input.sha256` 与本次快照一致；快照中不含其他 trial ID；`leak_check.final_action ∈ {send,blocked}` 且 blocked 未发送 | 上下文串台 → 涉事 trial invalid；裁判重跑 |
| I16 | 网络留痕完整且绕过可判定 | 出口代理记录 DNS/URL；阻断直连；`disable_web_search` 按策略关闭；策略文件哈希 | `dns_log_uri`/`url_log_uri` 存在且行数 ≥ 请求数；`bypass_attempts=0`；`policy_sha256` 与批次一致 | 绕过 → 按作弊（`zero`）或人工；无法判定 → manual |

### D.3 网络策略

##### 6.1 四种模式（与 `task.yaml network.mode` 逐字一致）

| 模式 | 语义 | 允许的出口 | 适用 | 强制实现 |
| --- | --- | --- | --- | --- |
| `off`（默认） | 完全断网 | 无（仅控制面 SSH 入向回包） | 绝大多数题 | Harbor `no-network`；沙箱内 egress 防火墙 drop；DNS 返回 NXDOMAIN 并留痕 |
| `docs_whitelist` | 只读官方文档 | 白名单域名 443（只读 GET） | 鸿蒙/Metal/Vulkan 等冷门 API 题 | Harbor `allowlist` + `allowed_hosts`；代理侧按 SNI/Host 校验 |
| `package_mirror` | 包镜像 + 文档白名单 | 只读包镜像 + 白名单文档站 | 必须装依赖但不想开放公网的题 | 代理把 registry 路径改写为内网只读镜像；**publish/上传类请求一律 403** |
| `full` | 全联网 | 全部 | 仅特殊题，需 Lead 书面批准并进批次记录 | `public`；仍全量留痕；仍屏蔽自家仓库/榜单域名 |

**默认值陷阱**：Harbor 环境基线默认是 `public`，`off` 不是默认值；题包必须显式写 `network_mode`，预检按 §5 I4/I16 断言实际断网与留痕。

**不允许的中间态**：不允许「只关掉 web search 但保留公网」；不允许临时放宽启动策略（Harbor 的 Modal Compose 说明：不会临时放宽受限的启动策略）。

##### 6.2 出口代理架构

```
沙箱（无默认路由 / 私有 netns）
   │ 唯一路由：10.x.y.1:3128（HTTP CONNECT 代理，仅控制面可配）
   ▼
出口代理（控制面）
   ├─ 策略引擎：mode + allowlist + 全局黑名单（自家仓库/榜单/题库域名/模型厂商控制台）
   ├─ 只读包镜像：pip/npm/cargo/ohpm 路径改写；POST/PUT/PATCH/DELETE → 403
   ├─ 搜索代理：只返回黑名单过滤后的结果；请求与响应全文留痕
   └─ 日志：dns.jsonl + url.jsonl（时间、进程、URL、状态、字节数、拦截原因）
```

##### 6.3 白名单与黑名单

| 规则 | 内容 | 例子 |
| --- | --- | --- |
| 白名单最小化 | 只放题目声明的域名；精确到主机名，路径前缀可选 | `developer.huawei.com`、`docs.huawei.com` |
| 全局黑名单（任何模式） | 自家仓库/榜单/题库/结果存储/对象存储域名 | `git.internal/*`、`leaderboard.internal`、`eval-artifacts.internal` |
| 搜索代理黑名单 | 搜索结果里剔除自家域名、题解站、paste 站；命中即不返回 | `gist.github.com`、`pastebin.com`、非白名单时的 `raw.githubusercontent.com` |
| 包镜像只读 | 只允许 GET/HEAD；禁止 publish、dist-tag、上传、yank | `npm publish`、`twine upload` |
| 元数据阻断 | 阻断云元数据与宿主服务地址 | `169.254.169.254`、`127.0.0.1:*`（除 trial 内 compose 服务）、宿主内网段 |

##### 6.4 留痕字段（I16 的数据）

`url.jsonl` 每行：`{ts, trial_id, round, process, method, host, path, status, bytes, blocked_reason}`；`dns.jsonl` 每行：`{ts, trial_id, qname, qtype, answer, blocked}`。日志由控制面采集，**不写进沙箱**（避免被 Agent 读取或篡改）。

### D.4 密钥与脑手分离

```
被测模型 API ◀── Model Gateway（唯一持有长期 key；计费/限流/供应商锁定/日志脱敏）
                     ▲ 短期 per-trial 凭证（租约 ID 可追溯，用完即废）
Agent 外壳（控制面，沙箱外）── SSH/ACP 单向进入 ──▶ 沙箱（无任何长期凭证）
```

| 规则 | 规定 | 验收 |
| --- | --- | --- |
| 长期密钥只在网关 | 模型 key、provider key、SSH 私钥、对象存储凭证只在控制面；Harbor 的 provider key（如 `DAYTONA_API_KEY`）也只留在 Harbor 进程 | 密钥扫描：沙箱内 `env`/文件/history 无命中 |
| per-trial 短期凭证 | 每 trial 一个 key 租约（网关侧），失效时间 = trial 最大墙钟 + 缓冲 | `isolation.secrets.key_lease_id` 非空；网关日志按 key 聚合只对应一个 trial |
| 预算硬上限 | 网关实时计费，达 `budget.max_cost_usd` 拒绝请求（`stop_reason=cost_cap`） | 网关日志与 `rounds[].usage.cost_usd` 可对账 |
| 日志脱敏 | 轨迹/网关日志/产物导出前替换 key、token、邮箱、绝对用户路径 | 抽检命中 0；`isolation.secrets.log_redaction=true` |
| 密钥事故门槛 | 任何长期凭证进入沙箱 = 事故；trial 立即 invalid + 轮换凭证 + incident | `api_key_in_sandbox=true` 触发 schema 条件 → invalid |
| 脑手分离 | 外壳与 LLM 调用在沙箱外，工具执行经 SSH/ACP；沙箱可完全断网 | `ssh_control_plane_only=true` |

**关于 ASP**：Agent Sandbox Protocol 目前是 **RFC 草案**（PR #3023，`asp.schema.json` v0，SSH 是首个 transport，已验证 provider 只有 Docker / Apple Container / Daytona），**P1 不依赖 ASP**；脑手分离先走 provider 原生的 exec/upload 接口，ASP 只作为后续可选路径（[ASP 文档](https://docs.harborframework.com/sandboxes/asp.md)）。

## 附录 E · 题库与题包契约

### E.1 题目包契约


**必需文件**（缺一不可，CI 会检查；本文写作之时尚未接入 CI，由 Lead 人工核对）：

| 文件 | 内容要求 |
| --- | --- |
| `task.yaml` | 元数据，字段对齐 task 元数据 Schema；必须含 id/version/track/platform/difficulty/hard_gates/rubric 引用/预算/资源策略/金丝雀位置 |
| `instruction.md` | 交给被测 Agent（或用户 Agent）的指令，真实用户口吻，不写实现提示 |
| `persona.md` | 模拟用户设定 + F0-F3 逐级可说话术 + 常见澄清问答（FAQ）+ 禁止泄露清单 |
| `rubric.yaml` | 原子化二元条目，字段对齐 rubric Schema，必含 `f1_symptom` |
| `defects.yaml` | 缺陷现象目录，字段对齐 defect Schema，供 F1/F2/F3 生成反馈 |
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

### E.2 三层分类法与 12 track

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

### E.3 题目元数据示例（macos-001/task.yaml）

```yaml
# macos-001 题目元数据（对齐 task 元数据 Schema v1.0）
id: macos-001
version: 1.0.0
track: macos
layer: platform
title: Metal 粒子渲染 20fps→60fps 优化
platform: bare-metal-macos
difficulty: 4
difficulty_calibration:
  status: pending
  target_tier_range: [A, B]
  models_tried: []
  notes: 本机（Apple M6 / macOS 27.2）实测：starter 24000 粒子 1080p 平均 57.0ms/帧（17.5fps）；参考解 A 0.167ms、B 0.105ms。门槛 16.7ms 对参考解余量约 100 倍；待 2-3 个前代模型实测后回填。
status: active
author: tasks-b
created: "2026-11-06"
updated: "2026-11-06"
language: [swift, metal]
tags: [macos, metal, performance, particles, gpu]

instruction: instruction.md
persona: persona.md
rubric_ref: rubric.yaml
defects_ref: defects.yaml

workspace:
  mount: /workspace
  starter: starter/
  entry_files: [README.md, Makefile, Sources/, tests/]
  protected_paths: [tests/]

resources:
  image: macos-27-xcode-27.2-metal
  image_digest: TODO(pin-on-host)
  cpu: 8
  memory_mb: 16384
  disk_mb: 40960
  os: macOS 27.2 (arm64, Apple Silicon)
  gpu: true
  network: false
  docs_whitelist: []
  tools: [bash, read, write, edit]
  services: []
  notes: 功能构建在 macOS 验证机/VM（Tart）内完成；性能指标必须在裸机性能机独占运行，VM 半虚拟化 GPU 数字不作数。

budget:
  max_rounds: 6
  max_feedback_rounds: 3
  max_nudges: 2
  max_total_tokens: 2000000
  max_wall_clock_s: 5400
  max_cost_usd: 22
  max_observe_s: 900
  escalation:
    f1_no_progress_rounds: 2
    f2_no_progress_rounds: 2

hard_gates:
  - id: HG1
    title: 性能门槛（5 次中位帧时间 <= 16.7ms）
    kind: script
    script: command:bash hidden-tests/run.sh
    required: true
    pass_condition: exit_code == 0
    description: 24000 粒子 / 1920x1080 / 120 帧，5 次独立运行 avg_frame_ms 中位数 <= 16.7ms（>=60fps）。
    timeout_s: 900
  - id: HG2
    title: 测试文件哈希未变
    kind: script
    script: hidden-tests/check_locks.py
    required: true
    pass_condition: exit_code == 0
    description: tests/ 下文件 sha256 与 locks.sha256 一致。
    timeout_s: 60
  - id: HG3
    title: 功能与输出契约
    kind: script
    script: hidden-tests/run.sh
    required: true
    pass_condition: exit_code == 0
    description: 构建成功、JSON 字段完整、非背景像素 >=1%、两次渲染 frame_hash 相同。
    timeout_s: 300

stop_conditions:
  success: 所有 hard_gates 通过，且无未修复的 functional/fatal 缺陷。
  failed: 预算耗尽或任一 hard_gate 失败（预算耗尽标注 budget_exhausted）。
  failure_budget: 轮数/token/墙钟/费用任一触顶即停止并判 Failed(budget_exhausted)。
  minor_affects: minor 缺陷（R7/R9）只影响 P/A 区分。

reference_solutions:
  - id: a
    path: answers/reference-a.patch
    approach: 合批 + 降同步（instanced 单次 draw、三缓冲常驻 instance buffer、DispatchSemaphore 不在 CPU 等 GPU）
    runnable: true
    notes: 本机实测 0.167ms/帧（5987fps），编译需 macOS + Xcode CLT。
  - id: b
    path: answers/reference-b.patch
    approach: 降低 CPU/片元开销（顶点着色器内动画、MTLHeap private 常驻缓冲、discard 提前剔除）
    runnable: true
    notes: 本机实测 0.105ms/帧（9540fps）；画面与基线同量级（非背景像素 434508 对 400727）。

references:
  a: answers/reference-a.patch
  b: answers/reference-b.patch
  empty_agent: answers/empty-agent.md
  patch_apply: patch -p1

artifacts:
  - path: Sources/ParticleBench/
    kind: source
    required: true
    description: 优化后的渲染实现源码。
  - path: build/ParticleBench
    kind: build_output
    required: false
    max_bytes: 10485760
    description: 本地构建产物（验证侧重新构建，不依赖它）。

isolation:
  agent_user: agent
  environment_mode: separate
  network_mode: no-network
  notes: Agent 以非 root 用户 agent 运行；Agent 环境与验证环境分离；默认完全断网。

canary:
  manifest: canary.md
  locations:
    - starter/README.md 末尾 HTML 注释行
    - starter/Sources/ParticleBench/Shaders.swift 注释行
    - starter/NOTICE.txt 构建标记行
  expected_trace:
    - agent.step 读取 README.md 与 Shaders.swift 的工具输出
    - 产物快照中 starter/README.md 的内容
    - agent 修改 Sources/ 时的 diff 上下文
  exclude_from_hash_lock: true

verification:
  entry: hidden-tests/run.sh
  selftest: bash hidden-tests/run.sh --selftest
  expected_runtime_s: 120
  timeouts_s:
    suite: 900
    single_case: 300

extensions:
  schema_note: 隔离声明按 task.schema v1.0 写在顶层 isolation；本包不依赖评测框架默认值。
  perf_host_requirement: 裸机性能机独占；测前 load1 < 2、LowPowerMode=0、温度正常；5 次取中位；VM 内数字不计分。
```

## 附录 F · 参考资料

Novita、Harbor、Tart、apple/container、Windows Sandbox、Hyper-V 等外部事实的 URL，见正文 §8 与 TECH-DESIGN §19。所有外部事实的快照日期为 2026-11-07，开工前按 TECH-DESIGN §18.2 的待核实清单复核。
