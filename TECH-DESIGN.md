# EvalSpleef · LLM Agentic 评测平台 · 技术与实现方案 v3

| 项 | 内容 |
| --- | --- |
| 项目名 | **EvalSpleef**（CLI：`spleef`；Python 包前缀：`evalspleef`；插件入口组：`evalspleef.*`） |
| 文档版本 | v3.0-draft |
| 日期 | 2026-11-07 |
| 状态 | 待评审 |
| 配套文档 | `PRD.md`（需求规格，同目录）。本文中的「PRD §x / 附录 X」均指该文件 |
| 自包含说明 | 不依赖仓库内其他文档；需要的契约原文已照录在 PRD 附录 A–E |
| 标注约定 | 【已核实】= 已对照官方文档/源码（URL 见 §19）；【推断】= 工程推断，需 PoC 验证；【待核实】= 文档未说明或矛盾，需问供应商或实测 |

---

## 1. 设计原则

1. **内核自有，供应商在边缘。** 调度、评测流、判定、隔离审计只依赖两套 SPI 和能力声明。供应商 SDK 只出现在 `adapters/` 下，用 import 规则强制隔离。
2. **能力声明优先于假设。** 每个适配器如实声明能力，调度按"需求 ⊆ 能力"匹配。不满足就在创建阶段拒绝，**绝不静默降级**。这一点沿用 Harbor 的做法。
3. **管理面与数据面分离。** 模型侧把"端点怎么来"（部署、扩缩、下线）和"怎么调用"（推理流量）拆开：
   - 前者可以是全自动、人工辅助或外部登记；
   - 后者一律经网关，走 OpenAI 兼容协议。
4. **每个 trial 从不可变模板新建，用完即毁。** 快照只用于加速同一模板的冷启动，不用于在 trial 之间传递状态。
5. **远程透明。** 本地机器上的载体经主机代理（hostd）暴露。对核心来说，它和云端载体是同一个 SPI。
6. **可复现字段在采集时就落库。** 包括模型修订号、端点探测报告、模板 digest、网关版本、外壳版本。不靠事后补。
7. **继承 v2 契约、不改枚举。** 状态、档位、反馈级别、错误分类、预算字段逐字沿用（PRD 附录 A）。v3 只做加法：新增字段、事件和适配器。

---

## 2. 关键架构决策（ADR-v3）

| # | 决策 | 理由 | 代价 / 风险 | 被否决的备选 |
| --- | --- | --- | --- | --- |
| ADR-v3-01 | 评测内核自有，Harbor 降为可选适配器，双向桥接 | Harbor 的 `TaskOS` 无 macOS、环境接口无快照、无 Novita Sandbox 类型；它约 3–4 周发一个版本，焊死在它上面风险大 | 失去部分"开箱即用"；外壳集成要自己维护或经桥接复用 | 保持 Harbor 主干：新载体都写成 `BaseEnvironment` 子类 |
| ADR-v3-02 | 模型侧 SPI 区分三种管理模式：`api` / `assisted` / `external` | Novita Dedicated Endpoint 目前没有公开的管理 API【已核实，2026-11-07】；但这件事随时可能变化，接口不应随之变化 | assisted 模式有人工步骤 | 用浏览器自动化操作控制台：脆弱，且可能违反服务条款 |
| ADR-v3-03 | 所有模型流量经自有网关；数据面采用 LiteLLM Proxy（锁版本），加自研控制插件负责 trial 密钥、预算、事件 | LiteLLM 已有 OpenAI ↔ Anthropic 等协议转换、虚拟密钥与预算、回调机制【推断：PoC 验证具体版本能力】 | 依赖第三方网关的正确性；需版本锁与回归 | 纯自研网关：可控但工期 +3 周。保留为回退方案 |
| ADR-v3-04 | 端点进入批次前必须通过能力探测，探测报告冻结进批次 | Dedicated Endpoint 的引擎参数不可见，工具调用等能力只能实测 | 每个端点多 5–10 分钟 | 相信文档或模型卡 |
| ADR-v3-05 | HF 权重"冻结镜像仓库"：批次所用修订同步到自有组织的私有仓库，部署指向镜像 | Novita 控制台没有修订号字段，扩容会重拉权重，必须保证内容不变 | 存储成本；镜像同步耗时 | 直接指向上游 repo，再赌它不更新 |
| ADR-v3-06 | 沙箱 SPI 必需操作只有 create / exec / upload / download / destroy；其余（快照、暂停、改网络、GUI、端口）均为能力项 | 用最小公共面覆盖 Windows Sandbox 到 Firecracker 这样差距极大的载体 | 高级能力要在调用方判断 | 一个大而全的接口 |
| ADR-v3-07 | 共享出口代理（egress proxy）作为平台组件 | Tart、Hyper-V、apple/container 只有 CIDR 或开关，而 I16 留痕要求 URL 级日志 | 多一个关键路径组件 | 各载体自行实现 |
| ADR-v3-08 | 两种执行模式：`remote-tools`（统一外壳默认）、`in-sandbox`（原生外壳默认） | 原生 CLI 只能在沙箱内跑工具；统一外壳可以做到完全脑手分离 | `in-sandbox` 需要沙箱能访问网关（短期密钥） | 只支持一种 |
| ADR-v3-09 | E2B 系载体（Novita Sandbox v2、E2B 自托管）共享适配器基类 | 二者 SDK 形态与语义同源（envd、commands/files 结构）【已核实：Novita 2.x wheel 内使用 envd 与 E2B 头】 | 需要隔离两者差异点 | 各写一套 |
| ADR-v3-10 | 本地载体通过 hostd 主动外连控制面（mTLS + gRPC 双向流） | Mac mini 和 Windows 宿主通常在 NAT 后，不开入站端口 | hostd 本身要高可用 | 控制面 SSH 进宿主 |
| ADR-v3-11 | 适配器以独立 Python 包发布，经 entry points 注册 | 核心与适配器独立发版；第三方可以写适配器 | 版本矩阵管理 | 单包内 if/else |
| ADR-v3-12 | 一致性套件（conformance kit）是适配器的"完成定义" | 把"新增供应商 ≤ 5 人日"变成可检查的条件 | 套件本身要维护 | 靠代码评审 |

---

## 3. 总体架构

### 3.1 全景

```
                         ┌──────────────────────────── 控制面（可信）────────────────────────────┐
  题包 / models.yaml ───▶ │ Registry（Task / Model / Template / Provider）                        │
  providers.yaml         │      │                                                                │
  batch.yaml             │      ▼                                                                │
                         │ Batch Planner ──▶ Scheduler（能力匹配 + 配额 + 公平） ──▶ Trial Runner │
                         │      │                 │                                  │    │      │
                         │      ▼                 ▼                                  │    │      │
                         │ Endpoint Manager   Lease Manager ◀── Inventory ◀── hostd 心跳 │    │      │
                         │ （模型侧 SPI）      （沙箱侧 SPI）                          │    │      │
                         │      │                 │                                  │    │      │
                         │ Probe Runner      Isolation Preflight / Canary            │    │      │
                         │      │                 │          Observer / Verifier ◀───┘    │      │
                         │      ▼                 ▼          Feedback Controller          │      │
                         │ ┌────────────── Model Gateway ──────────────┐  Judge Panel ─────┘      │
                         │ │ LiteLLM Proxy + evalspleef-gw 插件           │  Grader / Report          │
                         │ │ trial 虚拟密钥 · 预算 · 协议转换 · 计费    │  Result Store / Events    │
                         │ └───────────────────┬──────────────────────┘  Egress Proxy（共享）     │
                         └─────────────────────┼─────────────────────────────────┬───────────────┘
              模型面（推理）                    │                                 │ 沙箱面（执行，不可信）
  ┌────────────────────────────────────────────┴───────────┐   ┌─────────────────┴─────────────────────────────┐
  │ novita-dedicated  │ novita-serverless-gpu │ vpc-vllm     │   │ 云：novita-sandbox                             │
  │ openai-compatible（官方 API / Novita Serverless / 其他） │   │ VPC：e2b-selfhost · k8s-agent-sandbox          │
  └────────────────────────────────────────────────────────┘   │ 本地（经 hostd）：docker · tart · apple-container│
                                                                │   libvirt · hyperv · windows-sandbox            │
                                                                │   device-pool · perf-host                       │
                                                                └─────────────────────────────────────────────────┘
```

### 3.2 组件清单

v2 的 16 个组件 C1–C16 全部保留，职责不变。下表只列 v3 新增和变更的组件。

| # | 组件 | 职责 | 新增/变更 |
| --- | --- | --- | --- |
| C4′ | Sandbox Manager → **Lease Manager** | 经 `SandboxProvider` 创建、销毁实例；登记租约；孤儿回收 | 变更：不再直接依赖 Harbor |
| C7′ | Model Gateway | LiteLLM Proxy 加 `evalspleef-gw` 插件：trial 虚拟密钥、预算、协议转换、计费、供应商锁定、事件 | 变更：明确实现方案 |
| C17 | Model Registry | ModelSpec 的不可变版本、HF 来源、修订号、许可 | 新增 |
| C18 | Endpoint Manager | 经 `ModelEndpointProvider` 管理端点生命周期；assisted 模式的工单与校验；预热与缩容 | 新增 |
| C19 | Probe Runner | 对端点跑能力探测，产出 ModelCapabilities 与报告 | 新增 |
| C20 | Provider Registry | 加载适配器插件、实例化 provider 实例、缓存能力声明与隔离画像 | 新增 |
| C21 | hostd（主机代理） | 本地宿主上运行适配器，外连控制面，上报库存与心跳 | 新增 |
| C22 | Egress Proxy | 共享出口：域名允许列表、只读包镜像、URL/DNS 留痕 | 新增（v2 有设计，无组件） |
| C23 | Template Builder | 把题目环境定义构建为各载体的模板（OCI → Novita 模板 / E2B 模板；Tart 金镜像；Windows 金 VHDX），并登记 digest 映射 | 新增 |
| C24 | Conformance Kit | 适配器一致性测试（pytest 插件） | 新增 |
| C25 | Cost Ledger | 汇总 token 费用、GPU 端点时长、沙箱用量、账单导入，并对账 | 新增 |

---

## 4. 代码与仓库结构

技术栈如下：

- 语言与运行时：Python 3.12，asyncio，pydantic v2（配置与契约）。
- 存储：Postgres 16，S3 兼容对象存储（本地 MinIO / 云 S3）。
- 观测：OpenTelemetry。
- 通信：gRPC（hostd）。
- 测试：pytest。
- 依赖约束：沿用 v2 的"锁版本"策略，即 `constraints.txt` + 镜像 digest。

```
evalspleef/                           # monorepo
├── packages/
│   ├── evalspleef-core/              # 领域模型、状态机、判定、反馈、缺陷、金丝雀、报表（由 v2 参考实现迁入）
│   │   └── evalspleef/core/...
│   ├── evalspleef-spi/               # 只有接口与数据类：model.py / sandbox.py / runner.py / errors.py / caps.py
│   ├── evalspleef-control/           # registry、planner、scheduler、lease、endpoint manager、probe、cost ledger、API、CLI
│   ├── evalspleef-gateway/           # LiteLLM 插件（keys/budget/events）+ 部署清单
│   ├── evalspleef-runners/           # unified_shell（remote-tools）、native_shell（in-sandbox）、inspect_runner、harbor_runner
│   ├── evalspleef-hostd/             # 主机代理（gRPC 客户端 + 本地 provider 宿主）
│   ├── evalspleef-egress/            # 出口代理配置生成与日志采集
│   ├── evalspleef-conformance/       # 一致性套件（pytest plugin：pytest --spleef-provider=<name>）
│   └── adapters/
│       ├── evalspleef-model-openai-compat/
│       ├── evalspleef-model-novita/  # novita-dedicated + novita-serverless-gpu
│       ├── evalspleef-model-vpc-vllm/
│       ├── evalspleef-sbx-docker/
│       ├── evalspleef-sbx-e2b-family/   # 基类 + novita-sandbox + e2b-selfhost
│       ├── evalspleef-sbx-tart/
│       ├── evalspleef-sbx-apple-container/
│       ├── evalspleef-sbx-libvirt/
│       ├── evalspleef-sbx-hyperv/
│       ├── evalspleef-sbx-windows-sandbox/
│       ├── evalspleef-sbx-k8s-agent-sandbox/
│       ├── evalspleef-sbx-device-pool/  # 鸿蒙设备池 + 裸机性能机
│       └── evalspleef-harbor-bridge/
├── templates/                        # 各题环境定义（Dockerfile / Tart packer / Windows 应答文件）
├── deploy/                           # compose（M0）与 helm（M2+）
└── constraints.txt
```

**依赖规则**用 import-linter 在 CI 中强制：

- `evalspleef-core` 与 `evalspleef-control` 只能依赖 `evalspleef-spi`，不得 import 任何适配器包或供应商 SDK，例如 `novita_sandbox`、`e2b`、`docker`、`libvirt`、`harbor`。
- 适配器只能依赖 `evalspleef-spi` 与各自的供应商 SDK。

**插件注册**（各适配器的 `pyproject.toml`）：

```toml
[project.entry-points."evalspleef.sandbox_providers"]
novita-sandbox = "evalspleef_sbx_e2b_family.novita:NovitaSandboxProvider"
e2b-selfhost   = "evalspleef_sbx_e2b_family.selfhost:E2BSelfHostProvider"

[project.entry-points."evalspleef.model_providers"]
novita-dedicated      = "evalspleef_model_novita.dedicated:NovitaDedicatedProvider"
novita-serverless-gpu = "evalspleef_model_novita.serverless_gpu:NovitaServerlessGpuProvider"
```

---

## 5. 核心契约

### 5.1 继承的契约

以下契约逐字继承，不做修改，原文见 PRD 附录：

| 契约 | 附录 |
| --- | --- |
| trial 状态、反馈级别、档位、严重度与扣分、检查类型、错误分类、有效性、预算字段 | 附录 A.2 |
| trial ID 等标识符 | 附录 A.1 |
| 合法转移表 | 附录 B.2 |
| 事件信封 | 附录 B.8 |
| 档位规则表 | 附录 C.3 |
| 隔离不变量 | 附录 D.2 |

### 5.2 新增的公共类型（`evalspleef-spi/caps.py`）

```python
from enum import StrEnum, IntEnum
from dataclasses import dataclass, field
from typing import Literal


class OSFamily(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    HARMONY = "harmony"


class Arch(StrEnum):
    X86_64 = "x86_64"
    ARM64 = "arm64"


class Isolation(IntEnum):  # 与附录 D.1 的 L1–L4 一一对应；L0 禁止，不建模
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


class NetMode(StrEnum):  # 与题包 network.mode 逐字一致（附录 D.3）
    OFF = "off"
    DOCS_WHITELIST = "docs_whitelist"
    PACKAGE_MIRROR = "package_mirror"
    FULL = "full"


AllowKind = Literal["hostname", "wildcard", "ipv4", "ipv6", "cidr"]


@dataclass(frozen=True)
class RuntimeRequirements:  # 来自 task.yaml runtime.requirements（PRD FR-TSK-01）
    os: OSFamily
    arch: frozenset[Arch]
    min_isolation: Isolation
    network: NetMode
    allow_hosts: tuple[str, ...] = ()
    gpu: bool = False
    gui: bool = False
    cpus: float = 2
    memory_mb: int = 4096
    disk_mb: int = 10240
    max_lifetime_s: int = 3600
    execution_mode: Literal["remote-tools", "in-sandbox"] = "remote-tools"
```

### 5.3 错误分类（`evalspleef-spi/errors.py`）

| SPI 异常 | 含义 | 是否重试 | 映射 `error.class`（附录 A.2 §4.6） | `disposition` |
| --- | --- | --- | --- | --- |
| `CapabilityNotSupported` | 请求超出能力声明 | 否 | —（应在冻结期被拦住；运行期出现即 bug） | `manual` |
| `PolicyNotEnforceable` | 网络/资源策略无法强制 | 否 | `environment` | `manual` |
| `QuotaExceeded` | 供应商配额 / 宿主槽位不足 | 是（排队） | `environment` | `retry` |
| `SandboxCreateError` | 创建失败（镜像、模板、宿主） | 是，≤3 次，换实例 | `environment` | `retry` |
| `SandboxLost` | 实例在 trial 中途消失（宿主离线、TTL 到期、供应商回收） | 新 trial | `environment` | `discard` |
| `TransferError` | 上传/下载失败 | 是，≤3 次 | `environment` | `retry` |
| `EndpointUnavailable` | 端点不可达/SLEEPING 超时/5xx 持续 | 是（退避） | `provider` | `retry` |
| `EndpointMismatch` | 实际模型/供应商与登记不符 | 否 | `provider` | `discard` |
| `ProbeFailed` | 能力探测不过 | 否 | —（冻结期） | — |

命令超时**不是异常**：`ExecResult.timed_out=True`，由调用方按"模型行为"处理（附录 B.6）。

---

## 6. 模型层

### 6.1 SPI（`evalspleef-spi/model.py`）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Any


class ManagementMode(StrEnum):
    API = "api"  # 适配器可程序化 deploy/status/scale/teardown
    ASSISTED = "assisted"  # 人工在供应商控制台操作；适配器生成指引、校验、轮询
    EXTERNAL = "external"  # 只登记已有端点（官方 API、他人维护的服务）


@dataclass(frozen=True)
class HFSource:
    repo: str  # "org/name"
    revision: str  # 40 位 commit sha；登记时由分支/标签解析而来
    access: Literal["public", "gated", "private"]
    token_ref: str | None  # SecretRef，如 "vault://hf/eval-readonly"
    mirror_repo: str | None = (
        None  # 冻结镜像仓库（ADR-v3-05），如 "our-org/mirror-qwen3-32b-1a2b3c4"
    )
    adapter_repo: str | None = None  # LoRA："org/lora@sha"
    files_sha256: dict[str, str] = field(
        default_factory=dict
    )  # config/tokenizer/chat_template 等小文件哈希


@dataclass(frozen=True)
class VendorSource:
    vendor: str  # "anthropic" / "openai" / "deepseek" / "novita-serverless" ...
    model_id: str  # 厂商侧模型 ID，含日期版本


@dataclass(frozen=True)
class DeploymentSpec:
    adapter: str  # "novita-dedicated" / "openai-compatible" / ...
    account: str  # provider 实例名（同一适配器可多账号）
    params: dict[
        str, Any
    ]  # 适配器私有：gpu_type、gpu_count、min/max_replicas、max_concurrency、engine_args…


@dataclass(frozen=True)
class ModelSpec:
    slug: str  # 附录 A.1 规则
    spec_version: int  # 不可变；改动即 +1
    family: str  # 反同源判定（微调模型取基座家族）
    roles: frozenset[Literal["target", "judge", "user"]]
    source: HFSource | VendorSource
    deployment: DeploymentSpec
    declared_context_tokens: int
    declared_max_output_tokens: int
    pricing_ref: str  # 价格表条目，如 "ref:novita-serverless/qwen3-32b@2026-11"
    license: str
    owner: str


class EndpointState(StrEnum):
    REGISTERED = "registered"
    REJECTED = "rejected"
    AWAITING_OPERATOR = "awaiting_operator"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    PROBE_FAILED = "probe_failed"
    READY = "ready"
    WARM = "warm"
    SLEEPING = "sleeping"
    DEGRADED = "degraded"
    FAILED = "failed"
    RETIRED = "retired"


@dataclass
class EndpointHandle:
    endpoint_id: str  # 我们的 ID：ep_<slug>_<短哈希>
    model: ModelSpec
    state: EndpointState
    base_url: str | None  # 上游 URL（只在网关与 Endpoint Manager 可见）
    upstream_model: str | None  # 请求体中的 model 字段
    protocol: Literal["chat_completions", "responses", "anthropic_messages"]
    credential_ref: str  # SecretRef
    external_ids: dict[str, str] = field(default_factory=dict)  # 如 {"novita_deployment_id": "..."}
    observed: dict[str, Any] = field(default_factory=dict)  # 引擎/版本/GPU 等可观测信息


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    checks: tuple[tuple[str, bool, str], ...]  # (check_id, passed, detail)


@dataclass(frozen=True)
class DeploymentPlan:
    gpu_type: str | None
    gpu_count: int | None
    replicas_min: int
    replicas_max: int
    max_concurrency: int | None
    est_vram_gb: float | None
    est_cost_per_hour_usd: float | None
    operator_instructions_md: str | None  # assisted 模式：给操作员的逐步指引


class ModelEndpointProvider(ABC):
    name: str
    management_mode: ManagementMode

    @abstractmethod
    async def validate(self, spec: ModelSpec) -> ValidationReport:
        """登记前校验：凭证、HF 访问/许可/格式、配额。不得有副作用。"""

    @abstractmethod
    async def plan(self, spec: ModelSpec) -> DeploymentPlan: ...

    @abstractmethod
    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        """api：真正创建；assisted：返回 AWAITING_OPERATOR 的句柄；external：直接 VERIFYING。"""

    async def attach(
        self, handle: EndpointHandle, operator_input: dict[str, str]
    ) -> EndpointHandle:
        """assisted 专用：接收操作员贴回的 base_url / model / deployment_id，转 VERIFYING。"""
        raise NotImplementedError

    @abstractmethod
    async def status(self, handle: EndpointHandle) -> EndpointState: ...

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        raise NotImplementedError  # 仅 api 模式

    @abstractmethod
    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        """预热：发最小请求直至 WARM 或超时。"""

    @abstractmethod
    async def teardown(self, handle: EndpointHandle, mode: Literal["sleep", "terminate"]) -> None:
        """api：执行；assisted：生成操作工单并等待确认；external：仅标记 RETIRED。"""

    @abstractmethod
    def upstream(self, handle: EndpointHandle) -> dict[str, Any]:
        """返回网关路由条目（LiteLLM model_list 项），含 api_base、model、凭证引用、默认参数。"""
```

**推理面不在 SPI 里。** 推理一律经网关，用标准 OpenAI / Anthropic 客户端调用网关地址。适配器只负责把上游信息交给网关（`upstream()`）。这样，模型供应商的差异不会进入外壳、裁判和模拟用户的代码。

### 6.2 端点生命周期（Endpoint Manager）

```
            validate ok                 deploy()                attach()/api 就绪
REGISTERED ───────────▶ (plan) ───▶ AWAITING_OPERATOR ──────▶ VERIFYING ──probe ok──▶ READY ⇄ WARM
     │ validate fail                      │ 24h 未处理 → FAILED     │ probe fail              │  ▲
     ▼                                    │ (api 模式: DEPLOYING)   ▼                         ▼  │ wake()
  REJECTED                                ▼                     PROBE_FAILED             SLEEPING
                                       DEPLOYING ──失败──▶ FAILED                           │
                                                                                 teardown(terminate)
                                                                                            ▼
                                                                                         RETIRED
```

- `READY` 表示"探测通过、可被批次引用"。`WARM` 表示"最近 N 分钟内有成功请求、无冷启动"。
- Endpoint Manager 每 60 s 对被批次引用的端点做一次轻量健康请求，内容是 `max_tokens=1` 的固定提示。每次请求都会产生一点费用，因此只在批次 `Running` 期间执行。
- 批次联动规则（PRD FR-MOD-06）：
  - 冻结守卫：所有端点 `READY`。
  - `Running` 守卫：所有端点 `WARM`，且已达到所需副本数。所需副本数 = ceil(峰值并发 trial 数 × 每 trial 并发请求数 / max_concurrency_per_replica)。
  - `Graded` 之后：按 `retire_policy` 选择 `sleep` 或 `terminate`。

### 6.3 Model Gateway

**部署形态**：在 LiteLLM Proxy（版本锁定）中加载 `evalspleef-gw` 插件，后接 Postgres 存 key 与用量，可选 Redis 做限流计数。各部分的职责：

| 能力 | 实现 | 备注 |
| --- | --- | --- |
| 路由表 | Endpoint Manager 在端点 `READY` 时把 `upstream()` 写入 LiteLLM 的 model_list（经管理 API 或配置热加载）；对外的模型名统一为 `ep_<endpoint_id>` | 外壳与裁判看到的只有 `ep_...`，看不到上游 URL |
| 协议 | 对外提供三种协议：`/v1/chat/completions`、`/v1/responses`（P1）、`/v1/messages`（Anthropic 格式）；上游统一用 chat_completions，对官方 Anthropic/OpenAI 端点直通 | 【推断】LiteLLM 支持这些入口与转换；在 PoC 中按能力探测清单逐项验证 |
| trial 虚拟密钥 | 控制面在 `trial.leased → running` 时签发：`key = gw.issue(trial_id, endpoint_id, max_budget_usd, max_tokens, expires_at)`；trial 终态时吊销 | 映射附录 D.4 的"per-trial 短期凭证" |
| 预算 | 每请求前检查剩余额度；超限返回 HTTP 429，error.code 为 `cost_cap` / `token_cap`；外壳据此停止，停止原因见附录 B.3 | 价格表版本冻结进批次 |
| 计费 | 按冻结价格表计 token 费用（主口径）；自部署端点另记 `ref-price` 标识；GPU 实际费用由 Cost Ledger 按批次分摊 | PRD FR-MOD-08 |
| 默认参数 | 按端点注入：`max_tokens` 上限（Novita Serverless 要求必填）、`stream_options.include_usage=true`、推理开关等；参数白名单之外的不改 | 默认参数也属于"冻结版本" |
| 冷启动吸收 | 上游返回 503/超时，且端点为 `novita-dedicated`、处于 SLEEPING 窗口时，网关以 15 s 为间隔重试，最长 8 分钟；期间写 `endpoint.cold_start` 事件，并在响应头标注 `x-spleef-cold-start-ms` | 外壳侧看到的是一次慢请求；控制面从墙钟中扣除（FR-FLOW-06） |
| 供应商锁定 | 对 `openai-compatible` 聚合平台，校验响应 `model` 与 provider 字段；不符则抛 `EndpointMismatch` 并写事件 | 继承附录 B.6 |
| 日志 | 每请求一条 usage 事件：trial、round、endpoint、上游 model、in/out/cached/reasoning tokens、耗时、状态码；正文默认不在网关落盘，由 Runner 作为轨迹保存并脱敏 | — |
| 暴露面 | M0：仅本机或内网；`in-sandbox` 模式加 Novita Sandbox 时，网关需要公网 HTTPS 入口（M0 用 Cloudflare Tunnel 或一台小云主机反代），只接受 trial 虚拟密钥 | 见 §16 |

**回退方案**：若 PoC 表明 LiteLLM 在协议转换上有缺陷（工具调用、流式、推理内容），先把"转换层"限定为自研的薄代理，只覆盖 `chat_completions` ↔ `anthropic_messages`。其余保留 LiteLLM。

### 6.4 能力探测（Probe Runner）

探测**经网关**执行，用一个专用的探测 key，因此它同时验证"网关 + 上游"整条链路。探测报告 `probe_report` 不可变，批次冻结时引用其 ID。

| ID | 探测 | 方法 | 输出字段 |
| --- | --- | --- | --- |
| P-01 | 基础 chat | 固定提示，temperature=0 | `chat=true`、首 token 延迟 |
| P-02 | 流式与 usage | `stream=true` + `include_usage` | `streaming`、`usage_in_stream` |
| P-03 | 单工具调用 | 1 个函数 schema，提示必须调用 | `tool_calling>=single`、参数 JSON 合法率（5 次） |
| P-04 | 并行工具调用 | 需要两个独立调用的提示 | `tool_calling=parallel` |
| P-05 | 工具结果回填多轮 | 3 轮工具往返的固定剧本 | `multi_turn_tools` |
| P-06 | `tool_choice` 强制/禁止 | `required` / `none` | `tool_choice_modes` |
| P-07 | 结构化输出 | `json_schema` strict | `json_schema`（judge/user 角色必需） |
| P-08 | 推理内容 | 检查 `reasoning_content` 或等价字段、开关参数 | `reasoning_field`、`reasoning_toggle` |
| P-09 | 有效上下文 | 二分：在 N token 处放置"针"，要求原样复述；上限为声明值 | `context_tokens_effective` |
| P-10 | 最大输出 | 要求输出长文本，读 `finish_reason` | `max_output_tokens_effective` |
| P-11 | 复现性参数 | 同 `seed` 两次输出是否一致；`stop` 生效 | `seed_deterministic`、`stop_ok` |
| P-12 | 吞吐 | 并发 1 / 4 / 16 各 20 请求 | TTFT/TPS 的 p50/p95，用于计算所需副本数 |
| P-13 | 协议转换 | 经网关的 `/v1/messages`、`/v1/responses` 各跑 P-03 / P-05 | `protocols_ok` |
| P-14 | 身份 | 响应 `model`、`system_fingerprint`（若有）与登记一致 | `identity_ok` |
| P-15 | 指纹（P2，可选） | 20 条固定提示 greedy 输出的哈希；与同 revision 的参考部署对比 | `fingerprint`（部署一致性线索，不作硬门槛） |

**题目准入**：`task.runtime.model_requirements` 与 `ModelCapabilities` 比较，结果有三种：

- 满足：可运行。
- 不满足：该 cell 自动 `Skip`，原因写 `capability:<字段>`。
- 无法判定：`Manual`。

### 6.5 Hugging Face 模型处理

**登记流程**（`spleef models register`）：

1. **解析修订**：用 `huggingface_hub.HfApi.model_info(repo, revision=<分支或标签>, token=<部署身份令牌>)` 解析出 commit sha，此后只使用 sha。
2. **访问预检**，使用的 HF 身份必须与部署时绑定到 Novita 的令牌相同：
   - `model_info` 成功，即可读；
   - 能下载 `config.json`，借此确认 Gated 模型的许可已接受。Gated 模型未接受许可时会返回 403 或 gated 错误；
   - siblings 中存在 `*.safetensors`，且没有"只有 `.bin`"的情况；
   - `config.json` 中的 `architectures` 存在。
3. **小文件哈希**：下载 `config.json`、`generation_config.json`、`tokenizer_config.json`、`tokenizer.json`，以及 chat template（若是独立文件），写入 `files_sha256`。
4. **冻结镜像**（novita-dedicated 默认开启；其他适配器只要支持按 revision 拉取，就可以关闭）：
   - 在我们的 HF 组织下创建私有仓库，命名为 `mirror-<slug>-<sha7>`；
   - 用 `HfApi` 做服务端复制，或在 CI 中执行"下载 → 上传"，仅限该 sha 的文件；
   - 只读令牌只授权该镜像仓库；
   - 镜像仓库**不再推送**，组织策略只授予 bot 写权限，批次期间撤销写权限；
   - 许可：镜像只用于内部评测，不公开；Gated 模型须确认其许可允许内部复制。这一项列入 PRD 风险与 Q 清单，法务确认后启用。
5. **批次结束复核**：再次 `model_info(mirror_repo)`，确认 sha 未变；变化则开 incident。

**令牌**：

- 使用 HF 细粒度令牌，权限为只读，并限定到指定仓库。分两个：
  - "预检令牌"，在控制面使用；
  - "部署令牌"，绑定在 Novita 控制台。
  
  两者须属于同一 HF 账号或组织成员身份，否则预检结论不能代表 Novita 实际能否拉取。
- 令牌到期日写入注册表。到期前 14 天告警。批次冻结时检查：到期日 > 计划结束日 + 7 天。

### 6.6 模型适配器

#### 6.6.1 `openai-compatible`（MP-1，external）

- `validate`：检查凭证有效，调用 `GET /models`（若支持），确认模型存在。
- `deploy`：直接返回 `VERIFYING`。`status`：用健康请求判断。`teardown`：仅标记 `RETIRED`。
- 参数：`base_url`、`model`、`api_key_ref`、`extra_headers`、`protocol`、`rpm/tpm`、`provider_lock`（聚合平台使用，形式为期望的供应商字段与正则）。
- 覆盖：官方 API，Novita Serverless（`https://api.novita.ai/openai`【已核实】），自建 vLLM/SGLang，其他兼容服务。

#### 6.6.2 `novita-dedicated`（MP-2，assisted）

| SPI 方法 | 实现 |
| --- | --- |
| `validate` | §6.5 的 HF 预检；Novita API key 有效（调用任一只读 API，如 `GET https://api.novita.ai/openai/v1/models`）；账户余额与账单 API 可用（可选） |
| `plan` | 显存估算 = 权重字节（由 safetensors 元数据求和）× 1.0 + KV cache（上下文 × 层数 × 头维 × 2 × dtype × 目标并发）× 1.2 安全系数；据此在 GPU 目录（H100 80GB、H200 141GB、RTX 4090/5090 等）中选最小可行配置；生成操作员指引 Markdown（见下）与成本估算 |
| `deploy` | 创建 `AWAITING_OPERATOR` 句柄与工单（CLI 打印 + 可选 IM 通知），工单含指引与 24 h 期限 |
| `attach` | 接收 `base_url`、`model`、`deployment_id`：① base_url 必须是 `https://` 且主机属于 `*.novita.ai`；② 发 `max_tokens=1` 请求，确认 200 且 `model` 字段匹配；③ 转 `VERIFYING`，触发 Probe Runner |
| `status` | 由推理面推断：成功 → `WARM`；连接成功但响应持续 > 60 s，或返回特征性"服务未就绪"错误 → 视为 `SLEEPING` 冷启动；401/403 → `FAILED`（key 不属于部署所有者）；404 → `FAILED`（URL 或 model ID 错） |
| `wake` | 循环发健康请求，直到成功或超时（默认 10 分钟） |
| `scale` / `teardown` | 生成工单，内容为"在控制台把 Min Replicas 调到 X"或"Terminate 部署 Y"；操作员确认后，系统用一次健康请求复核（teardown 后应 404 或 401） |
| `upstream` | `{"model_name": "ep_<id>", "litellm_params": {"model": "openai/<upstream_model>", "api_base": <base_url>, "api_key": "os.environ/NOVITA_API_KEY_<account>"}}` |

**操作员指引模板**（由 `plan` 生成，示例）：

```markdown
## 部署 ep_qwen3-32b_7c1e（批次 2026-12-b1）
1. 打开 Novita 控制台 → Models APIs → Deployments → + New Deployment
2. Name：`eval-2026-12-b1-qwen3-32b`
3. Model：Hugging Face → `our-org/mirror-qwen3-32b-1a2b3c4`
   - 本仓库为 Private：确认已用 **eval-deploy** 身份点过 "Integrate HF Token"
4. GPU：1 × H100 SXM 80GB（估算显存 71 GB，余量 11%）
5. Autoscaling：Min Replicas = 3，Max Replicas = 3，Scale-down Delay = 300s
   （批次期间固定 3 副本；Graded 后改为 0）
6. Max Concurrency per Replica：16；Suffix Decoding：关闭
7. 部署进入 RUNNING 后，从 Quick Start 复制 base_url 与 model，执行：
   `spleef models attach ep_qwen3-32b_7c1e --base-url <...> --model <...> --deployment-id <...>`
预计费用：3 × $1.99/h；批次计划 14 h ≈ $83.6（不含部署与预热时间）
```

**记录到 `observed`**：

- 部署 ID、GPU 类型与数量、副本设置，均由操作员确认。
- 首次冷启动耗时。
- `/models` 返回的元数据（若有）。

推理引擎类型与版本由 Novita 自动匹配，且不可见，记为 `unknown`。Novita 控制台 Metrics 页提供 TTFT、TPOT、吞吐等指标，P1 阶段由操作员截图归档，或 Novita 开放 API 后自动采集。

**升级路径**：只要 Novita 提供部署 API，就在本适配器内实现 `deploy`/`status`/`scale`/`teardown`，并把 `management_mode` 改为 `API`。调用方无需改动。

#### 6.6.3 `novita-serverless-gpu`（MP-3，api）

需要指定引擎参数（如 `--tool-call-parser`、`--reasoning-parser`、`--max-model-len`、量化方式）时使用，生命周期全自动。

| SPI 方法 | 实现【已核实接口形态：Novita Serverless GPUs v2】 |
| --- | --- |
| `deploy` | `POST https://api.novita.ai/gpus/v2/endpoints`，请求体字段：`type: sync`；`name`；`product_id`（GPU 规格）；`image: vllm/vllm-openai@sha256:<digest>`（固定 digest）；`command`（vLLM 参数，含 `--model <mirror_repo> --revision <sha>`）；`envs`（含 `HUGGING_FACE_HUB_TOKEN`，值从密钥管理注入）；`worker_config: {min_replicas, max_replicas, max_concurrent_per_worker, gpu_num}`；`health_check: {path: /health, port: 8000}`；`ports`；`request_timeout` |
| `status` | `GET /gpus/v2/endpoints/{id}`，返回的 `status`、`url` 映射到 EndpointState |
| `scale` | `PUT /gpus/v2/endpoints/{id}` 修改 `worker_config` |
| `teardown` | `DELETE /gpus/v2/endpoints/{id}` |
| `upstream` | `api_base = <url>/v1` |

- 优点：修订号、引擎版本、参数全部可控、可记录，可复现性最好。
- 代价：需要自行维护 vLLM 镜像版本和每个模型的参数清单。
- 计费：按 worker 秒计费【已核实】。

#### 6.6.4 `vpc-vllm`（MP-4，P2，api）

- 形态：在自建 K8s（GPU 节点）上，每个端点一个 Deployment + Service；或者用 Novita GPU Instance API（`/gpu-instance/openapi/v1/gpu/instance/create`）起一台实例运行 vLLM。
- 与 6.6.3 共用"vLLM 启动参数生成器"（`engine_profiles/<model-family>.yaml`），区别只在资源编排。

### 6.7 成本模型（Cost Ledger）

```
trial.cost.agent      = Σ 请求 (in × p_in + out × p_out + cached × p_cache)   # 冻结价格表；自部署取 ref-price
batch.gpu_cost        = Σ 端点 (RUNNING 时长 × GPU 数 × 单价)                  # api 模式取供应商状态；assisted 取工单时间戳 + 账单 API
batch.sandbox_cost    = Σ 实例 (vCPU·s × p_cpu + GiB·s × p_mem)                # Novita Sandbox 计价
trial.gpu_alloc       = batch.gpu_cost × trial_tokens / batch_tokens(同端点)     # 辅口径
对账：|Σ ledger − 供应商账单| / 账单 < 5%                                       # Novita 账单 API 可按 productCategory 过滤【已核实】
```

---

## 7. 沙箱层

### 7.1 SPI（`evalspleef-spi/sandbox.py`）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Literal
from .caps import OSFamily, Arch, Isolation, NetMode, AllowKind, RuntimeRequirements


@dataclass(frozen=True)
class NetworkPolicy:
    mode: NetMode
    allow_hosts: tuple[str, ...] = ()  # 题目白名单 + 平台自动追加项（网关域名、出口代理）
    allow_cidrs: tuple[str, ...] = ()
    via_proxy: str | None = None  # 出口代理地址；非空时载体只放行该地址（ADR-v3-07）


@dataclass(frozen=True)
class Resources:
    cpus: float
    memory_mb: int
    disk_mb: int
    gpus: int = 0
    gpu_type: str | None = None


@dataclass(frozen=True)
class TemplateRef:
    kind: Literal[
        "oci", "novita_template", "e2b_template", "tart_image", "vhdx", "qcow2", "device_profile"
    ]
    ref: str  # 镜像名 / 模板 ID / 金镜像名
    digest: str  # sha256:...；同一题环境在不同载体上的模板由 Template Builder 登记映射


@dataclass(frozen=True)
class SandboxSpec:
    trial_id: str
    lease_id: str
    role: Literal["agent", "verifier", "judge"]  # verifier 必须是独立实例（附录 D.2 I10）
    os: OSFamily
    arch: Arch
    template: TemplateRef
    resources: Resources
    network: NetworkPolicy
    run_as_user: str = "agent"  # 非 root（附录 D.2 I6）
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # batch/trial/lease/provider，供孤儿回收
    max_lifetime_s: int = 3600
    gui: bool = False


@dataclass(frozen=True)
class SandboxCapabilities:
    os_families: frozenset[OSFamily]
    arches: frozenset[Arch]
    isolation: Isolation  # 申报值；须与隔离画像实测一致
    net_modes: frozenset[NetMode]
    allowlist_kinds: frozenset[AllowKind]  # 原生支持的白名单类型；不含 hostname 时需 via_proxy
    dynamic_network_policy: bool
    streaming: bool  # 增量 stdout/stderr
    stdin: bool
    snapshot: Literal["none", "fs", "memory"]
    snapshot_fanout: bool  # 一个快照创建多个实例
    pause_resume: bool
    gui: Literal["none", "vnc", "rdp"]
    gpu: bool
    port_expose: bool
    host_mounts: bool  # 有此能力也不在正式 trial 中使用（I5）；仅供开发
    enforces_limits: frozenset[Literal["cpu", "memory", "disk", "pids"]]
    max_concurrency: int | None  # 例：tart 每宿主 2；windows-sandbox 1
    max_lifetime_s: int | None  # 例：novita 付费档 10800
    max_resources: Resources | None
    typical_start_s: float  # 调度用的代价提示
    reset_cost_s: float  # "销毁 + 新建"的典型耗时


@dataclass
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    duration_s: float
    truncated: bool = False  # 超过 max_output_bytes 时截断（截断头尾保留）


OutputCallback = Callable[[Literal["stdout", "stderr"], bytes], None]


class Sandbox(ABC):
    id: str
    spec: SandboxSpec

    @abstractmethod
    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        """语义：
        - cmd 为 list 时不经 shell；为 str 时用 OS 默认 shell（linux/macos: /bin/sh -lc；windows: powershell -NoProfile -Command）。
        - timeout 到期必须杀掉整个进程组/作业对象，返回 timed_out=True、exit_code=124（POSIX 约定，Windows 同样映射）。
        - 不支持流式的载体在结束时一次性回调 on_output（capabilities.streaming=False）。
        """

    @abstractmethod
    async def upload(self, local: Path, remote: str) -> None:
        """文件或目录；目录以 tar 流传输（Windows 用 zip 或 tar.exe）；保留权限位，拒绝软链指向沙箱外。"""

    @abstractmethod
    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        """导出侧必须做产物净化（附录 D.2 I11）：调用方在本地对结果再校验一次。"""

    @abstractmethod
    async def destroy(self) -> None:
        """幂等；实例不存在视为成功。"""

    # ---- 能力项：不支持时抛 CapabilityNotSupported ----
    async def snapshot(self, name: str) -> str:
        raise NotImplementedError

    async def set_network(self, policy: NetworkPolicy) -> None:
        raise NotImplementedError

    async def gui_endpoint(self) -> str:
        raise NotImplementedError  # vnc:// / rdp:// / https://…

    async def expose_port(self, port: int) -> str:
        raise NotImplementedError

    async def describe(self) -> dict:
        return {}  # 宿主、实际资源、观测到的镜像 digest


class SandboxProvider(ABC):
    name: str  # 适配器名，如 "novita-sandbox"
    instance: str  # provider 实例名，如 "novita-us-phx-1"、"macmini-01"

    @abstractmethod
    def capabilities(self) -> SandboxCapabilities: ...

    @abstractmethod
    def isolation_profile(self) -> "IsolationProfile": ...  # §7.7

    @abstractmethod
    async def preflight(self) -> dict:
        """凭证、配额余量、宿主健康、模板可用性。供调度与告警使用。"""

    @abstractmethod
    async def create(self, spec: SandboxSpec) -> Sandbox:
        """校验 spec ⊆ capabilities；不满足抛 CapabilityNotSupported / PolicyNotEnforceable。"""

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        raise NotImplementedError

    @abstractmethod
    async def list(self, labels: dict[str, str]) -> list[dict]: ...

    @abstractmethod
    async def reap(self, labels: dict[str, str], older_than_s: int) -> int:
        """回收孤儿实例；返回回收数量。"""
```

**语义约定**（conformance kit 会逐条检查）：

1. `create` 返回时，实例已可执行命令。"就绪"由适配器负责，调用方不轮询。
2. 实例生命周期不得超过 `max_lifetime_s`。适配器必须用供应商侧 TTL（如 Novita `timeout`）或 hostd 侧定时器做兜底。
3. 路径约定：Linux/macOS 工作区固定为 `/workspace`；Windows 为 `C:\workspace`。为满足 I8，题目看到的工作区名由 Runner 随机化后软映射。
4. `exec` 的 `env` 与模板环境合并；适配器不得把任何控制面凭证放进实例环境。
5. 同一 `Sandbox` 对象可并发调用 `exec`，适配器保证线程安全。

### 7.2 能力协商与调度匹配

```python
def match(req: RuntimeRequirements, cap: SandboxCapabilities) -> list[str]:
    """返回不满足的原因列表；空列表即匹配。"""
    reasons = []
    if req.os not in cap.os_families:
        reasons.append(f"os:{req.os}")
    if not (req.arch & cap.arches):
        reasons.append("arch")
    if cap.isolation < req.min_isolation:
        reasons.append(f"isolation:{cap.isolation}<{req.min_isolation}")
    if req.network not in cap.net_modes:
        reasons.append(f"net:{req.network}")
    if (
        req.network in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR)
        and "hostname" not in cap.allowlist_kinds
        and not egress_proxy_available(cap)
    ):
        reasons.append("net:hostname-allowlist")
    if req.gpu and not cap.gpu:
        reasons.append("gpu")
    if req.gui and cap.gui == "none":
        reasons.append("gui")
    if cap.max_lifetime_s and req.max_lifetime_s > cap.max_lifetime_s:
        reasons.append("lifetime")
    if cap.max_resources and (
        req.cpus > cap.max_resources.cpus
        or req.memory_mb > cap.max_resources.memory_mb
        or req.disk_mb > cap.max_resources.disk_mb
    ):
        reasons.append("resources")
    return reasons
```

**路由**：`providers.yaml` 的 `routes` 按顺序列出候选 provider 实例（§14.2）。调度器的处理顺序：

1. 过滤出 `match()` 为空的候选。
2. 在其中挑选"有空闲配额"的第一个。
3. 若都没有空闲，进入等待队列。v2 的公平与优先级规则不变。

批次冻结时，对每个 cell 都做一次静态匹配。没有任何候选的 cell 直接标记 `Skip(capability:<原因>)`。

**配额**：每个 provider 实例维护一个分布式信号量，容量取以下几项中的最小值：`max_concurrency`、供应商配额（来自 `preflight`）、`providers.yaml` 中的人工上限。

- macOS VM 槽位按宿主计数，每宿主 2 个。
- Linux 实例按 CPU/内存 bin-packing 计算。

### 7.3 网络策略实现矩阵

| 载体 | `off` | `docs_whitelist` / `package_mirror` | `full` | URL 留痕（I16） | 实现要点 |
| --- | --- | --- | --- | --- | --- |
| docker | `--network none`（remote-tools）或 internal 网络 + 仅网关 | internal bridge + egress proxy sidecar；nftables 只放行代理 | 默认 bridge | 代理日志 | 同 v2 |
| novita-sandbox | `allow_internet_access=False` | 方案 A：`allow_out=[代理公网 IP]`，经我们的代理（推荐，可留痕）；方案 B：`allow_out` 直接写域名（原生，无 URL 日志） | 默认 | 方案 A 有 | in-sandbox 模式下追加网关域名 |
| e2b-selfhost | `allow_internet_access=False` | 同 novita 方案 A/B；自托管时可在 VPC 出口加代理 | 默认 | 有 | 沙箱子网禁止访问 VPC 其他网段与 169.254.169.254 |
| tart | `--net-softnet-block=0.0.0.0/0`（只留 hostd 管理通道） | `--net-softnet-allow=<代理 IP>/32`，其余 block；代理在宿主上 | NAT | 代理日志 | Softnet 只支持 CIDR【已核实】 |
| apple-container | `--network` 用 `--internal` 网络 | internal 网络 + 宿主代理 | 默认 | 代理日志 | 无域名白名单【已核实】 |
| libvirt | isolated network | isolated + 宿主代理 + nwfilter | NAT | 代理日志 | — |
| hyperv | Private vSwitch | Internal vSwitch + 宿主代理 + `Add-VMNetworkAdapterAcl` 只放行代理 | External vSwitch | 代理日志 | 【推断】ACL 细节需实测 |
| windows-sandbox | `<Networking>Disable</Networking>` | 不支持 | 默认 | 无 | 仅开发自测 |
| device-pool | 设备侧断网；编译容器同 docker | 同 docker | — | — | 继承 v2 |

**出口代理（C22）**：每个"网络域"部署一套，包括云端控制面、每台本地宿主、VPC 出口。代理按 SNI/Host 匹配允许列表，包镜像路径只读（POST/PUT 一律 403），日志写入控制面，日志字段沿用附录 D.3 的 `url.jsonl` / `dns.jsonl`。实现可选 Envoy（`sni_dynamic_forward_proxy` + RBAC）或 Squid（`ssl_bump peek` + `acl ssl::server_name`）。日志由控制面采集，不写入沙箱。

### 7.4 载体适配器

每个适配器按同一模板说明以下内容：能力声明要点、生命周期映射、exec 与文件、网络、隔离要点、已知限制。

#### 7.4.1 `e2b-family` 基类，含 `novita-sandbox` 与 `e2b-selfhost`

**基类负责的通用逻辑：**

- 创建时设置 `timeout = spec.max_lifetime_s + 300`。
- `commands.run(..., on_stdout, on_stderr, timeout, cwd, envs, user)` 映射到 `Sandbox.exec`。
- `files.write/read` 映射到上传/下载；目录先打 tar 包再上传，进入沙箱后解包。
- `kill()` 映射到 `destroy`。
- 实例 `metadata` 写入 labels，供 `list` 和 `reap` 按标签检索。
- 超时兜底：先在沙箱内用 `kill -- -<pgid>` 杀进程组，再判定超时。

两个子类的差异如下：

| 项 | `novita-sandbox` | `e2b-selfhost` |
| --- | --- | --- |
| SDK | `novita-sandbox==2.x`（锁定小版本）：`Novita().sandbox.create(template=..., timeout=..., metadata=..., allow_internet_access=..., network=...)`【已核实形态】 | `e2b`（锁版本），`E2B_DOMAIN` 指向自托管域名 |
| 认证 | `NOVITA_API_KEY` 只在控制面 / Runner 中使用 | 自托管 API key |
| 区域 | `us-phx-1`（v2，支持快照） | 自定义 |
| 模板 | Template Builder：`novita.template.new().from_image("<registry>/<img>@sha256:…", 凭证)` → `.set_user("agent")` → `run_cmd("移除 sudo")` → `build(t, name, cpu_count, memory_mb)` 得到 `template_id` | `e2b template build`，使用同一 Dockerfile |
| 快照 | `create_snapshot()`（v2，内存 + 文件系统），仅用于"依赖装好的基线"加速 | 同 E2B 快照语义【已核实：一对多】 |
| 能力上限 | 付费档：并发 100、≤ 8 vCPU / 8 GiB、20 GB、单会话 ≤ 3 h（文档值【已核实】；产品页写 24 h【待核实】） | 由自有硬件决定 |
| GUI | 桌面沙箱：`novita.desktop.create()` + VNC 流 | E2B desktop 模板【推断】 |
| GPU | 无 | 无（Firecracker 不支持 GPU 直通） |

**隔离要点（novita-sandbox）**：

- 隔离实现未公开（【待核实】），申报的 `isolation` 先填 `L3`。上线前必须用 conformance kit 的隔离子集（§15.1，C-ISO-*）实测；实测不通过时，按实测结果下调申报级别。
- E2B 系沙箱可能自动注入带有平台标识的环境变量（【推断】，例如 `E2B_SANDBOX` 一类）。这会违反 I8（身份去标识）。两步处理：
  - 预检时 `env` 全量扫描；
  - Runner 以 `env -i` 加白名单的方式启动 Agent 进程，并清理 agent 用户的 profile。
- 预置模板里的默认用户可能带 sudo，这会违反 I6。因此题目模板必须自建，并删除 sudo。

**与控制面的网络关系：**

- `remote-tools` 模式：只有 Runner 调用 Novita 控制 API，沙箱本身可以完全断网。
- `in-sandbox` 模式：沙箱需要访问网关公网地址，具体做法见 §8.3。

#### 7.4.2 `docker`（P0）

| 项 | 内容 |
| --- | --- |
| 驱动 | Docker Engine API（`aiodocker` 或 `docker` SDK）；Podman 走兼容 API |
| 创建 | 按 digest 拉取镜像；`--read-only` + tmpfs（`/tmp`、`/home/agent`、`/workspace` 用 overlay 卷）；`--user agent`；`--cap-drop ALL`；`--security-opt no-new-privileges`；`--pids-limit`；`--cpus` / `--memory` / `--storage-opt`；`--runtime runsc`（L2，可选） |
| exec | `exec_create` → `exec_start(stream=True, demux=True)` → `exec_inspect` 取 `ExitCode`。超时由适配器实现：外层用 `timeout --kill-after=5s` 包装，或在沙箱外向进程组发信号 |
| 文件 | `put_archive` / `get_archive`（tar 流） |
| 能力 | linux；arch 随宿主；L1/L2；net：off/allowlist（配合代理）/full；streaming ✓；snapshot `fs`（`docker commit`，仅模板加速）；gpu（nvidia runtime，可选） |

#### 7.4.3 `tart`（Mac mini，P1）

| 项 | 内容 |
| --- | --- |
| 驱动 | `tart` CLI，经 `asyncio.create_subprocess_exec` 在 hostd 内运行；目前无官方 Python SDK |
| 创建 | `tart clone <golden@digest> <trial-vm>`（APFS 写时复制；macOS 27+ 可用 `--stacked`）→ `tart set --cpu --memory` → `tart run --no-graphics --net-softnet --net-softnet-allow=<代理>/32 <vm> &` → 等待 Guest Agent 就绪 |
| exec | `tart exec <vm> -- <cmd>`（gRPC 流到 Guest Agent，返回退出码【已核实】）；以非管理员用户执行：`sudo -u agent -i`，sudo 只给 hostd 的管理账户，Agent 用户不在 sudoers 中；备选 SSH（`asyncssh`） |
| 文件 | `tar` 经 exec 的 stdin/stdout 传输；或者 SSH/SFTP |
| 销毁 | `tart stop` → `tart delete` |
| 能力 | macos、linux（guest）；arm64；L3；`max_concurrency` = 2（macOS guest，每宿主）；gui `vnc`；snapshot `memory`（`--suspendable`，不用于 trial）；gpu ✗（半虚拟化，不用于性能题）；allowlist_kinds = {cidr} |
| 金镜像 | 由 Template Builder 用 Packer/Tart 构建：预装 Xcode 等工具链，创建 `agent` 用户，安装 Guest Agent，然后推送到 OCI registry 并记录 digest |
| 已知限制 | 首次拉取约 25 GB；宿主磁盘需预留 ≥ 2 × 金镜像大小 + 写时复制增量；Tart 许可为 FSL-1.1-ALv2，内部使用需法务确认【已核实许可文本】 |

#### 7.4.4 `apple-container`（Mac mini，P2）

- 用 `container run/exec/cp/rm` CLI 管理容器；每个容器是一台独立的轻量 Linux VM，隔离级别 L3。不占用 macOS VM 的并发配额。
- 要求宿主为 macOS 26 及以上。网络只有"内部网络/开放"两档，需要白名单时经宿主代理转发。
- 适用场景：在 Mac mini 上运行 arm64 Linux 题，或者作为 Docker Desktop 的替代。

#### 7.4.5 `libvirt`（P2）

- 金镜像为 qcow2，每个 trial 新建 overlay（`backing_file`），trial 结束删除 overlay 即完成重置。
- 命令执行走 SSH（`asyncssh`），以获得流式输出。qemu-guest-agent 的 `guest-exec` 没有流式能力，只作为 SSH 不可用时的备用。
- 可承载 Windows guest（x86 宿主上的 KVM）。与 Hyper-V 二选一即可。

#### 7.4.6 `hyperv`（Windows，P1）

| 项 | 内容 |
| --- | --- |
| 运行位置 | Windows Server / Windows 11 Pro 宿主上的 hostd（Python 服务），在本机调用 PowerShell |
| 创建 | `New-VHD -Differencing -ParentPath <golden.vhdx>` → `New-VM -VHDPath ... -Generation 2` → `Set-VMProcessor -Count` / `Set-VMMemory` → 连接 vSwitch（按网络模式选择）→ `Start-VM` → 等待 OpenSSH 端口就绪 |
| exec | Windows OpenSSH Server + `asyncssh`，支持流式输出与退出码。默认 shell 为 `powershell -NoProfile -NonInteractive`。超时时用 Job Object，或 `taskkill /T /F` 结束整棵进程树 |
| 文件 | SFTP，目录打成 zip 或 tar 传输（Windows 10 1803 及以上自带 `tar.exe`）；无网络时备用 PowerShell Direct 的 `Copy-Item -ToSession/-FromSession`【已核实】 |
| 销毁 | `Stop-VM -TurnOff` → `Remove-VM -Force` → 删除差分盘 |
| 能力 | windows（另可承载 linux guest）；x86_64（ARM64 宿主需单独验证）；L3；gui `rdp`；snapshot `memory`（检查点，仅用于模板加速）；gpu `~`（GPU-P，暂不支持）；net：off/allowlist（代理）/full |
| 金镜像 | 用 autounattend 无人值守安装：创建非管理员用户 `agent`，安装 OpenSSH Server，仅允许控制面公钥登录；关闭自动更新、Defender 云上报与遥测（避免出网）；用 sysprep 封装；记录 sha256 |
| 隔离画像要点 | Windows 版预检：`whoami /groups` 不含 Administrators；`Get-SmbShare` 无宿主共享；`Get-NetFirewallProfile` 出站默认阻止（off 模式）；`Test-NetConnection` 外网失败；`Get-ChildItem Env:` 中无凭证、无评测标识 |
| 许可 | 由平台工程师确认：Windows Server Datacenter / 评估版 / VDA，并记入宿主清单 |

#### 7.4.7 `windows-sandbox`（P3，仅开发）

- 启动：`wsb start --config <xml>` 获取实例 ID；执行：`wsb exec --id <id> -c <cmd> -r ExistingLogin`；结束：`wsb stop`。
- 限制【已核实】：
  - 同一宿主同时只能运行一个实例；
  - `exec` 只返回退出码，不返回 stdout；
  - 使用 `ExistingLogin` 需要先 `connect` 建立登录会话。
- 适配器的变通：
  - 输出：把 stdout 重定向到映射目录中的文件，执行结束后读取，因此 `streaming=False`；
  - 白名单：只支持完全开网或完全断网，`allowlist_kinds` 为空。
- 因为上述限制，**不注册到正式批次的 routes**，只给出题人做本机自测。

#### 7.4.8 `k8s-agent-sandbox`（P3）

- 基于 kubernetes-sigs/agent-sandbox 的 CRD：`Sandbox`、`SandboxTemplate`、`SandboxWarmPool`。隔离通过 RuntimeClass 选择 gVisor 或 Kata。
- Python 客户端用 `k8s-agent-sandbox`【已核实】。
- 出网限制用 Cilium FQDN NetworkPolicy。
- 预热池里的实例必须从未运行过 trial（I14），所以只允许 claim，不允许归还。

#### 7.4.9 `device-pool` / `perf-host`（P2，继承 v2）

- 鸿蒙设备池：编译容器复用 `docker` 适配器，设备侧由 hostd 通过 `hdc` 操作。SPI 映射如下：
  - `create`：租用设备，并执行基线检查；
  - `exec`：在编译容器内执行受限命令，或使用设备代理提供的白名单动作；
  - `destroy`：执行归还卫生清单（附录 D.2 I14）。
- 性能机：不运行 Agent，只接受已签名的产物。`create` 创建临时用户；`destroy` 删除该用户并恢复基线。能力上标注 `isolation=L4`、`max_concurrency=1`。

#### 7.4.10 `remote`（hostd 代理，P0）

- 核心侧的 `RemoteProvider` 实现完整 SPI，把调用转发给目标 hostd 上的本地 provider，外部行为与本地调用一致。
- 流式输出经 gRPC server-stream 回传。
- 文件传输使用分块流，每块 4 MiB，附 sha256 校验。

### 7.5 主机代理 hostd

```
控制面 hostd-gateway  ◀════ mTLS gRPC 双向流（hostd 主动外连）════  hostd（Mac mini / Linux / Windows 宿主）
   │ Inventory / Lease / RemoteProvider                                  ├─ 本地 provider 实例：docker / tart / hyperv …
   │                                                                     ├─ 本地 egress proxy（网络域）
   │                                                                     ├─ 本地 TTL 守护（兜底销毁）
   │                                                                     └─ 指标与日志（OTel exporter）
```

| 职责 | 说明 |
| --- | --- |
| 注册 | 首次启动时用一次性注册令牌换取客户端证书（有效期 30 天，到期自动轮换）；上报宿主信息：机型、OS、CPU、内存、磁盘，以及本机已装的 provider 及其能力 |
| 心跳 | 每 15 s 一次，上报槽位占用、磁盘余量、温度/负载（性能机）；**连续 60 s 无心跳**，控制面将其标为 `lost`，该宿主上的 trial 全部转为 `failed_infra`（PRD FR-SBX-10） |
| 执行 | 接收 SPI 调用并在本地执行，结果流式回传；单机内的并发上限由 hostd 自行执行（例如 macOS 只开 2 个 VM） |
| 排空 | `spleef hosts drain <host>`：不再接收新租约，等现有 trial 结束；维护模式下拒绝一切调用 |
| 兜底 | 断网期间，本地 TTL 守护照常销毁过期实例；重连后上报已回收的实例清单 |
| 安全 | hostd 运行在专用低权账户下，只持有 provider 所需的最小本地权限，例如 Hyper-V Administrators 组；不持有任何模型或供应商密钥 |
| 升级 | 版本号写入 `trial.versions.hostd_version`；采用灰度升级，先排空再升级 |

### 7.6 模板供应链（Template Builder）

一份题目环境定义，产出多种载体的模板：

```
templates/<env-id>/
├── Dockerfile                 # Linux：唯一事实来源
├── macos.pkr.hcl              # macOS：Tart 金镜像（Packer tart 插件或脚本化）
├── windows/autounattend.xml   # Windows：金 VHDX
└── env.yaml                   # 元数据：OS、工具链版本、需要的用户、需移除的能力（sudo）

构建流水线（CI）：
Dockerfile ─build─▶ registry/<env>@sha256:D
                    ├─▶ novita.template.build(from_image=…@sha256:D) → novita_template_id
                    ├─▶ e2b template build → e2b_template_id
                    └─▶ (docker/apple-container/k8s 直接用 D)
macos.pkr.hcl ─▶ tart push registry/<env>-macos:tag → digest M
windows ─▶ golden.vhdx → sha256 W（分发到各 Windows 宿主，hostd 校验）

template_registry：(env_id, env_version) → {oci: D, novita: id, e2b: id, tart: M, vhdx: W}
```

- **跨载体一致性检查**：每个模板构建完成后，都要在对应载体上执行 `env-fingerprint` 脚本，内容包括包清单哈希、工具链版本、用户与权限、PATH，并与 OCI 基线比对。只有差异在允许范围内（例如供应商注入的守护进程）才登记为可用。
- 批次冻结时记录 `template_registry` 快照。

### 7.7 隔离画像与预检

**画像**是每个适配器随代码一起交付的一份 YAML，内容是 I1–I16（原文见附录 D.2）中每一条的满足方式、预检命令和豁免项。示例节选如下：

```yaml
provider: novita-sandbox
declared_isolation: L3
verified_isolation: null          # conformance C-ISO-* 实测通过后由 CI 写入
invariants:
  I1: {how: "每 trial 新建实例，kill 后不复用；metadata 含 lease_id", probe: "list(labels)==1"}
  I2: {how: waived, reason: "VM 级一次性实例；根盘可写但实例销毁即弃", compensating: "关键路径哈希比对 + 金丝雀", approved_by: "<评测负责人>", expires: "2027-03-01"}
  I4: {how: "allow_internet_access=False 或 allow_out=[proxy]", probe: "curl --max-time 3 https://example.com 失败"}
  I6: {how: "模板 set_user(agent) 且移除 sudo", probe: "id -u != 0 && ! sudo -n true"}
  I8: {how: "Runner 以 env -i + 白名单启动", probe: "env | grep -Ei 'e2b|novita|eval|harbor|task_id' 为空"}
  I16: {how: "allow_out 仅代理 IP；URL 日志由代理采集", probe: "代理日志行数 ≥ 请求数"}
```

**预检探针集**按 OS 家族分别维护：

- `probes/linux.sh`：沿用 v2 中 I1–I9 的命令；
- `probes/macos.sh`：`id -u`、`sudo -n true`、`mount`、`scutil --get ComputerName`、断网测试；
- `probes/windows.ps1`：命令见 §7.4.6。

**执行方式**：控制面通过 `exec` 把探针脚本以 stdin 送入沙箱运行。不挂载宿主目录，结果写入 `trial.isolation.checks[]`。

**豁免**的规则：必须写明理由和补偿措施，由评测负责人批准，并设置到期日。每期隔离体检报告列出当期生效的全部豁免。

---

## 8. Agent 运行时（Runner）

### 8.1 Runner SPI

Runner 沿用 v2 的 `AgentShell` 语义（start / send / stop），v3 补充两点：执行模式，以及模型接入改为走网关。

```python
class AgentRunner(ABC):
    name: str  # 外壳 slug（附录 A.1），如 "terminus" / "opencode" / "claude-code"
    version: str
    supported_modes: frozenset[Literal["remote-tools", "in-sandbox"]]
    required_protocol: Literal["chat_completions", "responses", "anthropic_messages"]

    @abstractmethod
    async def start(
        self,
        sandbox: Sandbox,
        *,
        gateway_url: str,
        trial_key: str,
        model_alias: str,
        instruction: str,
        budget: "Budget",
    ) -> None: ...
    @abstractmethod
    async def send(self, message: str, *, deadline_s: int) -> "ShellTurn": ...  # ShellTurn 同 v2
    @abstractmethod
    async def stop(self) -> None: ...  # 幂等，清进程组
```

### 8.2 `remote-tools`（统一外壳的默认模式）

```
Runner 进程（控制面或 hostd 上）                         沙箱（可完全断网）
  ├─ Agent 循环：LLM ⇄ 网关（trial_key）
  └─ 工具调用 ──bash/read/write/edit──▶ Sandbox.exec / upload / download ──▶ /workspace
```

- 工具集与 v2 统一外壳一致：bash、read、write、edit、可选 browser（经 Observer）。每个工具都是对 SPI 的一次调用：
  - `bash(cmd)` → `exec(cmd, cwd=/workspace, user=agent, timeout_s=…)`
  - `read(path)` → `exec(["cat", path])`；大文件用 download
  - `write/edit` → upload 临时文件 + `exec(mv)`，保证原子替换
- 统一外壳可复用 Terminus 或 OpenCode 的 Agent 循环，以库或子进程方式运行。在它们的工具层下方插入"SPI 工具后端"即可。
- 密钥不进沙箱，沙箱可完全断网，满足 v2 ADR-3（脑手分离）。

### 8.3 `in-sandbox`（原生外壳的默认模式）

```
沙箱（模板内预装外壳，版本固定）
  外壳进程 ──HTTPS──▶ 网关公网入口（仅此一个出口；trial_key 认证）──▶ 上游端点
  环境变量（示例，【推断】具体变量以各外壳当期文档为准）：
    Claude Code : ANTHROPIC_BASE_URL=<gw>/  ANTHROPIC_AUTH_TOKEN=<trial_key>  ANTHROPIC_MODEL=ep_<id>
    Codex CLI   : config.toml model_providers.spleef = {base_url=<gw>/v1, env_key=SPLEEF_KEY, wire_api=...}
    OpenCode    : provider.spleef = {npm: "@ai-sdk/openai-compatible", options.baseURL=<gw>/v1}
```

- 网络策略：`allow_hosts` 中自动追加网关域名。题目本身是 `off` 时，最终策略为"只放行网关"；题目是 `docs_whitelist` 时，为"网关 + 白名单（经代理）"。
- `trial_key` 绑定单个 trial 和单个端点，带预算上限，到期失效，trial 终态时立即吊销。按附录 D.4，它属于"per-trial 短期凭证"，允许出现在沙箱内。
- 外壳的内置 web 搜索、遥测、自动更新一律关闭（模板中配置）。如果某外壳无法关闭，在 `env-fingerprint` 中标记，并在网络层阻断。
- 外壳自带的"会话记忆"文件（如 `~/.claude`）在模板中清空，避免跨 trial 残留（I3）。

### 8.4 外壳 × 模型组合准入

| 组合 | 条件 |
| --- | --- |
| 统一外壳 × 任意模型 | 能力探测 P-03/P-05 通过（工具调用与多轮回填） |
| 原生外壳 × 其自家模型 | 直通协议，P-01/P-02 通过 |
| 原生外壳 × 自部署开源模型 | P-13 中对应协议的转换探测通过；榜单标注"经协议转换"；首期只作对照实验，不进主榜（PRD Q4） |

---

## 9. Harbor 适配器（双向）

### 9.1 方向一：我们的沙箱 → Harbor 环境（`harbor-bridge`）

目的：Harbor 任务可以跑在任意一个我们的载体上，例如 `harbor run -e evalspleef_harbor_bridge:SpleefEnvironment --ek provider=novita-sandbox`。

| Harbor `BaseEnvironment` 方法【已核实签名】 | 桥接实现 |
| --- | --- |
| `type()` | 返回 `"evalspleef"` |
| `start(force_build)` | 由 `task_env_config`（镜像、cpus、memory_mb、storage_mb、gpus）与 `network_policy` 组装 `SandboxSpec`。镜像转换为模板：先查 template_registry，查不到且 `force_build=True` 时调用 Template Builder。最后 `provider.create(spec)` |
| `stop(delete)` | `sandbox.destroy()`；`delete=False` 时同样销毁，因为我们不保留实例 |
| `exec(command, cwd, env, timeout_sec, user)` | `sandbox.exec(...)`，结果映射为 Harbor `ExecResult(stdout, stderr, return_code)` |
| `upload_file / upload_dir` | `sandbox.upload` |
| `download_file / download_dir` | `sandbox.download` |
| `capabilities` | 由 `SandboxCapabilities` 映射：`disable_internet` ⇐ OFF ∈ net_modes；`network_allowlist_hostnames` ⇐ hostname ∈ allowlist_kinds（或有代理）；`dynamic_network_policy`；`gpus`；`windows` ⇐ WINDOWS ∈ os_families；`stream` |
| `set_network_policy(policy)` | `sandbox.set_network`，仅在能力支持时可用 |

限制：Harbor 的 `TaskOS` 只有 linux 和 windows，因此 macOS 载体只能通过我们自己的 Runner 使用，不能经桥接给 Harbor 任务。

### 9.2 方向二：Harbor 作为执行器（`harbor_runner`）

目的：复用 Harbor 的原生外壳集成（`-a claude-code` 等）、`harbor job regrade` 和 `harbor analyze`。

- 调用方式：`harbor run -p <转换后的 task> -a <shell> -m <LiteLLM 模型串指向网关> -e evalspleef_harbor_bridge:SpleefEnvironment --ek provider=<实例>`。
  - 模型串使用 OpenAI 兼容形式，`api_base` 指向网关，`api_key` 为 trial_key，因此 Harbor 的模型流量同样受网关计费和预算约束。
- 反馈循环仍在我们的控制器里。这与 v2 的结论一致："Harbor 直线 + 控制器循环"：每轮调用一次 Harbor 的单次运行，或者只把 Harbor 的外壳安装与启动逻辑当作库来复用，具体选哪种在 PoC 中决定。
- 版本：Harbor 版本锁定（v2 锁定 0.25.x），写入 `trial.versions.harbor`。每次升级都先跑桥接冒烟测试。

---

## 10. 编排、判定与观察

| 环节 | v3 实现 |
| --- | --- |
| 状态机 | 沿用 v2 `TrialMachine`（附录 B.2）。新增事件只追加，不改转移表 |
| 租约 | `leased` = 已向 provider 实例申请到配额并 `create` 成功 + 已签发 trial_key；任一失败 → 按 §5.3 映射 |
| 预检 | `leased → running` 的守卫：隔离预检（§7.7）+ 金丝雀注入（沿用 v2 `CanaryRegistry`）+ 端点 WARM |
| 观察 | Observer 通过 `Sandbox.exec` 运行可见验收；需要截图时，有 GUI 能力的载体走 `gui_endpoint`，无 GUI 的载体在沙箱内运行无头浏览器或 Xvfb |
| 验证 | 用 `role=verifier` 新建独立实例，可以是另一个 provider 实例，只要能力匹配；只上传声明的产物与隐藏测试（I10/I11）；验证实例 `network=off` |
| 判定 | 沿用 v2 的 `compute_grade`、`FeedbackLadder`、`DefectTracker`、`LeakChecker`（附录 C）。裁判与模拟用户模型经网关调用，模型名为 `ep_*` |
| 裁判面板 | `JudgePanel` 的成员来自注册表中 `roles ∋ judge` 的端点；反同源规则按 `ModelSpec.family` 判定；k=5 采样及一致性阈值不变（附录 C.5） |
| 逻辑赛道 | Inspect AI 作为 Runner 适配器：`inspect eval --model openai/ep_<id>`，通过 `OPENAI_BASE_URL` 指向网关；不需要沙箱 |
| 清理 | `destroy` 后写 `teardown.completed`；hostd 或供应商 TTL 负责兜底；每 10 分钟调用一次 `reap(labels={batch})` |

**典型时序**（remote-tools 模式、novita-dedicated、novita-sandbox）：

```
Scheduler   EndpointMgr   Gateway   LeaseMgr/novita-sandbox   Runner(统一外壳)   Observer/Verifier   Grader
   │ cell 可派发 │             │               │                       │                  │              │
   │──确认 ep WARM──▶│          │               │                       │                  │              │
   │──申请配额────────────────────────────────▶│ create(spec)         │                  │              │
   │                 │──issue(trial_key)──▶│    │◀─实例就绪─            │                  │              │
   │──预检 + 金丝雀（exec probes）────────────▶│                       │                  │              │
   │──start(sandbox, gw, key)─────────────────────────────────────────▶│                  │              │
   │                 │            │◀──LLM 请求（ep_*，计费/预算）────────│                  │              │
   │                 │            │               │◀──工具 exec/upload──│                  │              │
   │                 │            │               │                       │──stop: completed │              │
   │──observe────────────────────────────────────────────────────────────────────────────▶│              │
   │◀─缺陷/硬门槛───────────────────────────────────────────────────────────────────────────│              │
   │  （按 F0–F3 控制器循环，附录 C.2）                                                                     │
   │──verify：新建 verifier 实例，上传产物+隐藏测试──────────────────────────────────────────▶│              │
   │──destroy 两个实例；revoke key────────────▶│                                                             │
   │──compute_grade────────────────────────────────────────────────────────────────────────────────────────▶│
```

---

## 11. 数据与存储

在 v2 数据模型（task/batch/cell/trial/round/observation/defect/feedback/judge_verdict/grade/artifact/lease/canary/incident/audit_event）的基础上，新增以下表。

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `model_spec` | slug、spec_version、family、roles、source（JSON）、deployment（JSON）、declared_*、pricing_ref、license、owner、created_at | 不可变；改动即新版本 |
| `hf_artifact` | repo、revision、mirror_repo、files_sha256、safetensors_bytes、license、verified_at | 冻结镜像与复核记录 |
| `endpoint` | endpoint_id、model_slug+version、adapter、account、state、external_ids、observed、created_at、retired_at | 生命周期投影（权威来源为事件） |
| `endpoint_ticket` | ticket_id、endpoint_id、kind（deploy/scale/teardown）、instructions_md、status、operator、opened_at、closed_at | assisted 模式工单 |
| `probe_report` | report_id、endpoint_id、gateway_version、results（JSON）、capabilities（JSON）、created_at | 不可变；批次冻结时引用 |
| `provider_instance` | instance、adapter、version、capabilities（JSON）、isolation_profile（YAML 存档）、verified_isolation、config_hash | 能力声明与画像存档 |
| `host` | host_id、instance（hostd）、os、hw、hostd_version、state（active/draining/lost/maintenance）、last_heartbeat | 本地宿主清单 |
| `template_build` | env_id、env_version、oci_digest、provider_template_ids（JSON）、fingerprints（JSON）、status | 模板注册表 |
| `sandbox_lease`（扩展） | + provider_instance、host_id、template_kind/ref/digest、role（agent/verifier）、created_at、destroyed_at、reaped | 每 trial 至少 agent + verifier 两条 |
| `gateway_key` | key_id（不存明文）、trial_id、endpoint_id、budget、expires_at、revoked_at | per-trial 密钥 |
| `cost_ledger` | batch、kind（tokens/gpu/sandbox/infra）、source、amount_usd、quantity、unit、period、reconciled | 成本对账 |

`trial.versions` 在 v2 字段上新增：`model_source`、`hf_repo`、`hf_revision`、`hf_mirror_repo`、`deployment_adapter`、`deployment_id`、`endpoint_id`、`probe_report_id`、`inference_engine`、`gpu_type`、`quantization`、`context_length_effective`、`chat_template_sha256`、`gateway_version`、`sandbox_adapter`、`sandbox_adapter_version`、`provider_instance`、`template_digest`、`hostd_version`、`runner_mode`、`harbor`（若使用）。

---

## 12. 可观测、审计与成本

- **Trace**：每个 SPI 调用都是一个 OTel span，属性包括 `trial_id`、`provider_instance`、`op`、`bytes`、`exit_code`、`timed_out`；网关请求的 span 挂在 Runner 的轮次 span 之下。
- **指标**（Prometheus）：
  - 端点：`endpoint_state`、`endpoint_cold_start_seconds`、`endpoint_idle_seconds`；
  - 沙箱：`sandbox_create_seconds{provider}`、`sandbox_active{provider}`、`sandbox_orphans_reaped_total`；
  - 宿主：`host_heartbeat_age_seconds`；
  - 网关：`gateway_budget_rejections_total`、`gateway_upstream_errors_total{endpoint,code}`。
- **告警**：与 PRD FR-OPS-03 一一对应，阈值放在配置中。
- **审计**：事件只追加，信封格式见附录 B.8。新增事件类型见 PRD FR-FLOW-04。
- **成本报告**：每批次生成一份，内容包括"预计 vs 实际"、按模型与按 provider 的明细、空转占比，以及对账差异。

---

## 13. 安全

| 资产 | 位置 | 规则 |
| --- | --- | --- |
| Novita API key、厂商 API key | 密钥管理（Vault / 云 KMS / 1Password Connect，任选一种，通过 `SecretRef` 抽象接入） | 只有网关、Endpoint Manager 和 Runner 能读取；从不进入沙箱 |
| HF 预检令牌、部署令牌 | 密钥管理；部署令牌另外绑定在 Novita 控制台 | 细粒度只读，限定仓库；记录到期日并告警 |
| Novita Sandbox 管理凭证 | 控制面与 Runner | 不进入沙箱；沙箱内的 `Sandbox-Traffic-Access-Token` 等访问令牌只由 Runner 持有 |
| trial_key | 网关签发 | 可进入沙箱（in-sandbox 模式）；绑定 trial、端点和预算，到期失效，trial 终态时吊销 |
| hostd 证书 | 宿主本地 | 30 天轮换；一机一证，可单独吊销 |
| 轨迹与产物 | 对象存储 | 导出前脱敏，规则沿用 v2；按保留策略清理 |

- **网关公网入口**：只接受 trial_key，per-key 限流，以 WAF 或 IP 信誉作为基础防护；管理 API 只在内网开放。
- **供应商侧数据**：在 Novita 等平台上，确认端点不开启会留存请求的功能，并在合同或条款层面确认训练数据使用政策（列入待核实清单）。

---

## 14. 配置示例

### 14.1 `models.yaml`

```yaml
models:
  - slug: qwen3-32b-ft-sql           # 自部署：私有微调模型
    family: qwen
    roles: [target]
    source:
      type: huggingface
      repo: our-org/qwen3-32b-sql-sft
      revision: 3f9c2b7d1e...        # 必须 40 位 sha；登记时可写分支，CLI 解析后回写
      access: private
      token_ref: vault://hf/eval-preflight
      mirror: true                   # 冻结镜像（ADR-v3-05）
    deployment:
      adapter: novita-dedicated
      account: novita-main
      params:
        gpu_type: H100-SXM-80GB
        gpu_count: 1
        replicas: {min: 3, max: 3, after_batch: 0}
        max_concurrency_per_replica: 16
        suffix_decoding: false
    declared_context_tokens: 131072
    declared_max_output_tokens: 16384
    pricing_ref: ref:novita-serverless/qwen3-32b@2026-11
    license: apache-2.0
    owner: model-ops

  - slug: llama-4-maverick           # 自部署：Gated 模型，需定制工具调用解析器 → 全自动路径
    family: llama
    roles: [target]
    source: {type: huggingface, repo: meta-llama/Llama-4-Maverick-17B-128E-Instruct, revision: "<sha>", access: gated, token_ref: vault://hf/eval-preflight, mirror: true}
    deployment:
      adapter: novita-serverless-gpu
      account: novita-main
      params:
        product_id: "<H200 规格 ID>"
        gpu_num: 8
        image: vllm/vllm-openai@sha256:<digest>
        engine_args: ["--tool-call-parser", "<parser>", "--max-model-len", "131072"]
        replicas: {min: 1, max: 2}
    declared_context_tokens: 131072
    declared_max_output_tokens: 8192
    pricing_ref: assumed:llama-4-maverick@2026-11
    license: llama4-community
    owner: model-ops

  - slug: claude-sonnet-5            # 官方 API：裁判
    family: claude
    roles: [judge, target]
    source: {type: vendor, vendor: anthropic, model_id: "<dated-model-id>"}
    deployment:
      adapter: openai-compatible
      account: anthropic-official
      params: {protocol: anthropic_messages}
    declared_context_tokens: 200000
    declared_max_output_tokens: 64000
    pricing_ref: api:anthropic@2026-11
    license: proprietary
    owner: eval-lead
```

### 14.2 `providers.yaml`

```yaml
model_accounts:
  novita-main:        {adapter: novita-dedicated, api_key_ref: vault://novita/main}
  anthropic-official: {adapter: openai-compatible, base_url: https://api.anthropic.com, api_key_ref: vault://anthropic/eval}

sandbox_instances:
  novita-us-phx-1:
    adapter: novita-sandbox
    config: {region: us-phx-1, api_key_ref: vault://novita/main, sdk: "novita-sandbox==2.1.*"}
    limits: {max_concurrency: 80}            # 低于账号配额 100，留余量给 verifier
  macmini-01:
    adapter: remote
    config: {host: macmini-01, providers: [tart, docker]}
    limits: {tart: {max_concurrency: 2}, docker: {max_cpus: 8, max_memory_mb: 24576}}
  win-hv-01:
    adapter: remote
    config: {host: win-hv-01, providers: [hyperv]}
    limits: {hyperv: {max_concurrency: 4}}
  local-docker:
    adapter: docker
    config: {runtime: runc}

routes:                                      # 按顺序尝试，前提是 match() 通过
  - when: {os: linux}
    prefer: [novita-us-phx-1, local-docker, macmini-01/docker]
  - when: {os: macos}
    prefer: [macmini-01/tart]
  - when: {os: windows}
    prefer: [win-hv-01/hyperv]
  - when: {role: verifier}
    prefer: [local-docker, novita-us-phx-1]

egress_proxies:
  cloud:      {url: http://egress.internal:3128, public_ip: 203.0.113.10}
  macmini-01: {url: http://192.168.64.1:3128}
gateway:
  internal_url: http://gateway.internal:4000
  public_url: https://gw.example.com        # in-sandbox 模式 + 云沙箱时需要
```

### 14.3 `batch.yaml`（节选）

```yaml
batch: 2026-12-b1
models: [qwen3-32b-ft-sql, llama-4-maverick, claude-sonnet-5]
shells: {unified: terminus, native: {claude-sonnet-5: claude-code}}
tasks: {include: [logic-*, data-*, debug-*, backend-*, web-*, macos-001]}
epochs: 3
budget: {max_total_usd: 8000}
endpoint_policy: {warm_before_run: true, retire_after: sleep}
judges: [claude-sonnet-5, "<另一家族裁判>"]
pricing_table: 2026-11
```

---

## 15. 测试策略

### 15.1 Conformance Kit（适配器完成定义）

运行方式：`pytest --spleef-provider=<instance> -m "<能力标签>"`。kit 会根据适配器的能力声明自动选择适用用例；对于声明了某能力却跑不通的情况，判为失败。

| 组 | 用例 | 适用条件 |
| --- | --- | --- |
| 生命周期 | C-LIFE-01 create→exec→destroy；C-LIFE-02 destroy 幂等；C-LIFE-03 超过 `max_lifetime_s` 后被回收；C-LIFE-04 labels 可被 list/reap 检索 | 全部 |
| 执行 | C-EXEC-01 退出码；C-EXEC-02 stdout/stderr 分离；C-EXEC-03 超时杀整棵进程树（含后台子进程）；C-EXEC-04 流式顺序与完整性；C-EXEC-05 超大输出截断；C-EXEC-06 cwd/env/user 生效；C-EXEC-07 并发 exec | 全部（04 仅 streaming） |
| 文件 | C-FILE-01 文本/二进制往返；C-FILE-02 目录往返，保留权限位；C-FILE-03 100 MB 文件；C-FILE-04 拒绝指向沙箱外的软链 | 全部 |
| 网络 | C-NET-01 `off` 时外网不可达；C-NET-02 白名单内可达、白名单外不可达；C-NET-03 不支持的策略在 create 阶段被拒；C-NET-04 元数据地址（169.254.169.254）不可达；C-NET-05 运行期改策略 | 按 net_modes / dynamic |
| 隔离 | C-ISO-01 新实例看不到上一个实例写入的 marker；C-ISO-02 非 root 且无 sudo；C-ISO-03 env 与文件系统中无凭证、无评测标识；C-ISO-04 无宿主可写挂载；C-ISO-05 资源限额生效（按 enforces_limits）；C-ISO-06 金丝雀注入与扫描闭环 | 全部 |
| 并发与故障 | C-CONC-01 不超过 max_concurrency；C-FAIL-01 宿主/实例中途消失时抛 `SandboxLost`；C-FAIL-02 供应商 5xx 时按 `SandboxCreateError` 重试 | 全部 |
| 可选 | C-SNAP-01 快照创建与恢复；C-SNAP-02 一对多；C-GUI-01 取得 GUI 端点并截图；C-PORT-01 端口暴露 | 按能力 |

模型侧 kit：

| 组 | 用例 |
| --- | --- |
| API | M-API-01 chat；M-API-02 流式与 usage；M-API-03 错误码映射（401/404/422/429/5xx） |
| 身份 | M-ID-01 model 字段与登记一致 |
| 生命周期 | M-LIFE-01..04 deploy/status/scale/teardown，仅 api 模式；M-ASSIST-01 assisted 工单流：生成指引 → attach → 校验失败路径（错误 base_url / 错误 model / 403） |
| HF | M-HF-01 公开/gated/private 预检：gated 未授权、private 无权限、`.bin` only 三种失败都给出明确原因 |

### 15.2 其他测试

| 层 | 内容 |
| --- | --- |
| 单元 | 继承 v2 的 131 个单测（判定、状态机、反馈、金丝雀）；新增 SPI 数据类、match()、路由、预算、价格计算 |
| Mock 适配器 | `mock-sandbox`（内存文件系统 + 子进程）、`mock-endpoint`（可编排响应：慢启动、429、工具调用格式错误）；核心集成测试只用 Mock |
| 契约测试 | 每个真实适配器在 CI nightly 中对真实环境跑 conformance kit；Novita 部分使用专用测试账号，设置预算上限 |
| 端到端 | M0：3 题 × 1 模型 × 1 epoch × 2 载体；M1 起每次发版跑"金丝雀批次"（10 题） |
| 故障注入 | 宿主断网 / 断电、端点 SLEEPING、网关重启、Novita 配额耗尽、令牌过期；验收标准：受影响 trial 正确作废重排，其余成绩不受影响（NFR-05） |
| 安全演练 | 沿用 v2 红队演练；新增"in-sandbox 外壳尝试用 trial_key 调用非绑定端点 / 超预算 / 过期后调用"三项 |

---

## 16. 部署拓扑

### 16.1 M0：单台 Mac mini + Novita

```
Mac mini（Apple Silicon, ≥ 32 GB 内存, ≥ 1 TB SSD）
├── docker compose：postgres · minio · gateway(LiteLLM+evalspleef-gw) · evalspleef-control · egress-proxy · otel-collector
├── hostd（本机）→ providers: docker、tart（≤2 macOS VM）
└── Runner（remote-tools）
外部：Novita Dedicated Endpoint（被测模型）· Novita Agent Sandbox（Linux 题）· 官方 API（裁判）
```

- remote-tools 模式下，Mac mini 只需要出站访问：Novita API、HF、官方 API，不需要任何入站。
- 如果要在 Novita Sandbox 里跑 in-sandbox 原生外壳，沙箱必须能访问网关，因此需要为网关开一个公网入口。可选两种方式：
  - Cloudflare Tunnel 或类似的出站隧道；
  - 一台小型云主机做反向代理。
  
  只接受 trial_key。

### 16.2 M2+：生产

```
云（建议与 Novita us-phx-1 同在美西，降低时延）
├── K8s：evalspleef-control（多副本）· gateway（多副本 + Redis）· egress-proxy · hostd-gateway · runner 池
├── 托管 Postgres · S3 · 密钥管理
└── VPC：e2b-selfhost（裸金属 KVM 节点）· vpc-vllm（GPU 节点，可选）
本地机房/办公室
├── Mac mini × N（hostd：tart + docker/apple-container）· 性能机（hostd：perf-host）
├── Windows 宿主 × M（hostd：hyperv）
└── 鸿蒙设备池宿主（hostd：device-pool）
```

**容量估算**沿用 v2 公式。示例：Linux 侧 46 路并发，按 2 vCPU / 4 GiB 计，Novita Sandbox 费用约 46 × $0.12/h ≈ $5.5/h；12 h 批次约 $66，verifier 实例另计。自部署被测端点按 §6.2 的副本公式计算：

- 每个 trial 同一时刻约 1 个请求；
- 46 路 ÷ 16 并发/副本 ⇒ 3 副本；
- 1 × H100 时，费用为 3 × $1.99/h ≈ $6/h。

---

## 17. 实施计划

**人力假设**：3 名工程师。E1、E2 负责平台；E3 负责模型接入与运维。周期 14 周，产能约 210 人日；下表合计 163 人日，其余约 45 人日作为评审、联调、供应商沟通与缓冲。出题与标定人力另计。每人每阶段负荷控制在该阶段周数 × 5 人日以内。

| 阶段 | 周 | 工作包 | 负责 | 人日 | 产出 / 闸门 |
| --- | --- | --- | --- | --- | --- |
| **M0 最小闭环** | 1–4 | W01 SPI 包（model/sandbox/runner/errors/caps）+ v2 参考实现迁入 core | E1 | 6 | `evalspleef-spi` 0.1；v2 单测全绿 |
| | | W02 Postgres 数据模型 + 事件存储 + Scheduler v0（能力匹配 + 信号量） | E2 | 8 | 迁移脚本；match() 单测 |
| | | W03 网关 PoC：LiteLLM 锁版本 + evalspleef-gw（trial key、预算、usage 事件）；协议转换逐项验证 | E3 | 8 | PoC 报告；不达标则切回退方案（§6.3） |
| | | W04 `openai-compatible` + Probe Runner（P-01..P-12） | E3 | 6 | 探测报告格式冻结 |
| | | W05 `novita-dedicated`（assisted）+ HF 预检 + 工单 CLI | E3 | 6 | 公开模型部署端到端 |
| | | W06 `docker` 适配器 + Linux 预检探针 | E2 | 4 | conformance（基础子集）通过 |
| | | W07 `novita-sandbox` 适配器 + Template Builder（OCI→Novita 模板） | E2 | 7 | 同题在 docker / novita 上一致 |
| | | W08 统一外壳 remote-tools（SPI 工具后端） | E1 | 6 | 3 类题跑通 |
| | | W09 端到端：logic / data / debug 小批次 | 全员 | 3 | **闸门：PRD AC-01（公开模型）、AC-03** |
| **M1 生产化核心** | 5–8 | W10 hostd + RemoteProvider + mTLS 注册/心跳/排空 | E1 | 8 | 宿主断网演练通过 |
| | | W11 `tart` 适配器 + macOS 金镜像流水线 + macOS 探针 | E2 | 7 | 2 路并发 24 h |
| | | W12 Conformance Kit 完整版（沙箱 + 模型） | E1 | 6 | CI nightly |
| | | W13 in-sandbox 原生外壳（claude-code / codex-cli / opencode 模板）+ 网关公网入口 | E1 | 6 | P-13 探测通过的组合可跑 |
| | | W14 Egress Proxy + 网络矩阵（docker / tart / novita 方案 A） | E2 | 5 | C-NET-* 通过 |
| | | W15 HF 冻结镜像流程 + Gated/Private 预检 + versions 扩展 | E3 | 5 | AC-01 全部 |
| | | W16 Cost Ledger + Novita 账单导入 + 空转告警 | E3 | 5 | 对账误差报告 |
| | | W17 隔离画像（docker / novita / tart）+ 体检报告按载体分节 | E3 | 4 | **闸门：AC-02、AC-04、AC-06** |
| **M2 扩展载体** | 9–11 | W18 `hyperv` 适配器 + Windows 金镜像 + Windows 探针 + 1 个 Windows 题包 | E2 | 9 | AC-05 |
| | | W19 `e2b-selfhost`（VPC 部署 E2B infra + 子类适配器） | E1 | 8 | conformance 通过 |
| | | W20 `harbor-bridge` + `harbor_runner` | E1 | 6 | AC-08 |
| | | W21 `novita-serverless-gpu`（vLLM 引擎配置生成器） | E3 | 5 | 全自动部署 Gated 模型 |
| | | W22 首个正式批次运营（预热、监控、缩容、报告） | 全员 | 8 | **闸门：AC-10、NFR-04** |
| **M3 冷门与常态化** | 12–14 | W23 `libvirt` + `apple-container` | E2 | 6 | conformance 通过 |
| | | W24 设备池 / 性能机接入 SPI（迁移 v2 设计） | E1 | 8 | v2 P2 闸门 |
| | | W25 新增供应商演练（由未参与核心开发的工程师接入一家，如 E2B 托管或 Daytona） | 轮值 | 5 | **AC-07** |
| | | W26 加固：故障注入、红队演练、文档 | 全员 | 8 | 发布 v3.0 |

**关键路径**：W03（网关）→ W04/W05（模型）→ W09；W10（hostd）→ W11（tart）。W03 风险最高，排在第 1 周启动。

**各阶段人日核对**：M0 = 54（E1 12 / E2 19 / E3 20 / 共担 3），M1 = 46（E1 20 / E2 12 / E3 14），M2 = 36（E1 14 / E2 9 / E3 5 / 共担 8），M3 = 27（E1 8 / E2 6 / 轮值 5 / 共担 8）。

---

## 18. 风险、开放问题与待核实清单

### 18.1 技术风险

| 风险 | 触发信号 | 预案 |
| --- | --- | --- |
| 网关协议转换（Anthropic/Responses ↔ Chat）在工具调用、流式、推理内容上不完整 | P-13 失败率高 | 原生外壳 × 开源模型不进主榜；必要时对 `anthropic_messages` 自研薄转换层 |
| Novita DE 自动选的引擎不支持某模型的工具调用格式 | P-03 失败 | 该模型改走 `novita-serverless-gpu` 并指定解析器（PRD Q2） |
| E2B 系沙箱注入的环境变量或守护进程破坏 I8 | C-ISO-03 失败 | `env -i` 启动 + profile 清理；仍不行则在画像中写豁免与补偿措施 |
| Tart 金镜像过大，导致磁盘与分发瓶颈 | 宿主磁盘告警 | 用 `--stacked`（macOS 27+）分层；宿主之间用本地 registry 缓存 |
| hostd 成为单点 | 心跳抖动 | 每宿主一个 hostd，彼此独立；控制面侧的 hostd-gateway 多副本；心跳丢失只影响本宿主 |
| Windows 镜像自动更新或遥测导致出网和漂移 | 预检出网失败 | 金镜像中关闭这些功能；`off` 模式由 vSwitch 物理断网兜底 |

### 18.2 待核实清单（开工第 1–2 周内关闭）

| # | 事项 | 方式 | 负责 |
| --- | --- | --- | --- |
| V-01 | Novita DE 是否有未公开/企业版的部署管理 API；当前规范的推理 base_url 格式 | 联系 Novita 技术支持 / 商务 | E3 |
| V-02 | Novita DE 上工具调用 / logprobs / reasoning_content 的支持情况，以及自动匹配的引擎版本是否可查询 | 部署 2 个模型并跑探测 + 问 Novita | E3 |
| V-03 | Novita DE 的计费起点（文档前后矛盾），以及 4090 价格冲突 | 小规模实测 + 账单 API | E3 |
| V-04 | Novita DE 的 HF 拉取是否支持指定 revision（若支持，冻结镜像可降为可选） | 问 Novita / 实测 | E3 |
| V-05 | Novita Sandbox 的隔离实现、单会话最长时长（3 h 还是 24 h）、企业配额、注入的环境变量、默认用户的 sudo 情况 | 问 Novita + conformance C-ISO-* | E2 |
| V-06 | Novita Sandbox 的 `allow_out` 域名规则匹配方式（SNI / DNS？）与日志能力 | 实测 | E2 |
| V-07 | LiteLLM（选定版本）对 `/v1/messages`、`/v1/responses` 转换、虚拟密钥预算、回调的实际表现 | W03 PoC | E3 |
| V-08 | 各原生外壳指向自定义网关的配置方式（当期版本） | 实测 | E1 |
| V-09 | Gated 模型许可是否允许内部私有镜像；Tart（FSL）内部商业使用；Windows guest 许可 | 法务 | 评测负责人 |
| V-10 | Novita 对评测请求数据的留存与训练使用政策 | 条款 / 合同 | 评测负责人 |

---

## 19. 参考资料（2026-11-07 快照）

**Novita AI**
- Dedicated Endpoint 指南：<https://docs.novita.ai/guides/llm-dedicated-endpoint>
- 文档索引（API 参考范围）：<https://docs.novita.ai/llms.txt>
- Dedicated Endpoint 产品页（价格）：<https://novita.ai/dedicated-endpoint>
- 2025-07 博客（base URL 示例）：<https://blogs.novita.ai/deploy-custom-base-model-with-llm-dedicated-endpoint/>
- LLM API 与 Chat Completion 参数：<https://docs.novita.ai/guides/llm-api>、<https://docs.novita.ai/api-reference/model-apis-llm-create-chat-completion>
- Serverless GPUs v2 创建端点：<https://docs.novita.ai/api-reference/gpus-v2-create-endpoint>；vLLM 快速上手：<https://docs.novita.ai/guides/serverless-gpus-quickstart-create-endpoint>
- GPU Instance 创建：<https://docs.novita.ai/api-reference/gpu-instance-create-instance>
- Agent Sandbox：SDK/CLI <https://docs.novita.ai/guides/sandbox-sdk-and-cli>；总览/区域 <https://docs.novita.ai/guides/sandbox-overview>；生命周期 <https://docs.novita.ai/guides/sandbox-auto-resume>；命令 <https://docs.novita.ai/guides/sandbox-commands>；文件 <https://docs.novita.ai/guides/sandbox-filesystem>；网络 <https://docs.novita.ai/guides/sandbox-internet-access>；安全访问 <https://docs.novita.ai/guides/sandbox-secured-access>；模板 <https://docs.novita.ai/guides/sandbox-template-build-operations>；快照 <https://docs.novita.ai/guides/sandbox-snapshot>；配额 <https://docs.novita.ai/guides/sandbox-quota-limit>；价格 <https://docs.novita.ai/guides/sandbox-pricing>；桌面 <https://docs.novita.ai/guides/sandbox-computer-use>；遗留 E2B 兼容 <https://docs.novita.ai/guides/sandbox-legacy-e2b-compatible>

**沙箱与虚拟化**
- Harbor 环境接口：<https://github.com/harbor-framework/harbor/blob/main/src/harbor/environments/base.py>；能力：<https://github.com/harbor-framework/harbor/blob/main/src/harbor/environments/capabilities.py>；自定义环境：<https://docs.harborframework.com/sandboxes/custom-sandboxes.md>；网络策略：<https://docs.harborframework.com/tasks/network-policies.md>
- Tart：<https://github.com/openai/tart>；macOS VM 许可/上限：<https://developer.apple.com/forums/thread/845455>
- apple/container：<https://github.com/apple/container>
- Lume：<https://cua.ai/docs/lume/reference/cli-reference>
- E2B 自托管基础设施：<https://github.com/e2b-dev/infra>；E2B 快照：<https://docs.e2b.dev/sandbox/snapshots.md>
- kubernetes-sigs/agent-sandbox：<https://agent-sandbox.sigs.k8s.io/docs/getting_started/overview/>
- gVisor：<https://gvisor.dev/docs/user_guide/install/>；Firecracker：<https://github.com/firecracker-microvm/firecracker>
- Windows Sandbox CLI：<https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli>；PowerShell Direct：<https://learn.microsoft.com/en-us/virtualization/hyper-v-on-windows/user-guide/powershell-direct>
- Anthropic sandbox-runtime（进程级隔离参考）：<https://github.com/anthropic-experimental/sandbox-runtime>

