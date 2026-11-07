# 隔离预检清单（开工前）

> 状态：v1 · 维护：isolation · 最后更新：2026-11-06
> 上游：[00-conventions](../../00-conventions.md) · 相关：[04-isolation](../../04-isolation.md) · [trial-hygiene](trial-hygiene.md) · [incident-response](incident-response.md)

## 用途

批次冻结前（Draft → Frozen）与**每个 trial 启动前**（pending → leased → running）各执行一遍。任一项未通过，不得冻结批次 / 不得进入 running；豁免必须由 Lead 书面批准并记录在体检报告 §waivers。

## A. 批次期前检查（每期一次，冻结前）

### A1. 题目包与镜像

- [ ] 所有题目包必需文件齐全（`task.yaml`/`instruction.md`/`persona.md`/`rubric.yaml`/`defects.yaml`/`answers/*`/`hidden-tests/run.sh`/`canary.md`/`notes.md`），字段对齐 [task.schema.json](../../../schemas/task.schema.json)
- [ ] 每道题的 `platform` 与沙箱矩阵（[04-isolation](../../04-isolation.md) §3.2）一致；macOS/鸿蒙/性能题没有落到 L1
- [ ] 所有镜像按 digest 固定：`grep -L '@sha256:' tasks/*/task.toml` 输出为空
- [ ] 题包**显式**写 `[verifier] environment_mode = "separate"`（新建任务默认 separate，老任务默认 shared，不能依赖默认）
- [ ] 题包**显式**写 `[agent] user = "agent"`（Harbor 默认不创建 agent 用户，非 root 是 opt-in）
- [ ] 题包**显式**写 `[environment]/[agent] network_mode` 与 `network.mode` 一致
- [ ] `canary.md` 埋点位置与 `task.yaml canary.locations` 一致；埋点不落在 `hidden-tests/`、`answers/`、`tests/`
- [ ] 每道题有 `artifacts` 声明（否则验证侧收不到产物，且 `harbor job regrade` 无法重判）
- [ ] 隐藏测试未出现在 Agent 镜像/工作区说明中（`harbor lint` 规则 `agent-image-test-leak` 无命中）

~~~bash
harbor lint --extend-select unpinned-image --extend-select root-agent ./tasks/web-001
harbor lint --extend-select ALL ./tasks/*        # 体检期用 ALL，输出归档
harbor check ./tasks/web-001 --rubric ./rubrics/task-quality.md
~~~

通过标准：0 error；`unpinned-image` / `root-agent` 显式开启后 0 命中；`harbor check` 无 blocking 项。

### A2. 网络策略

- [ ] 每题的 `network.mode` 已冻结（`off` 为默认；`docs_whitelist`/`package_mirror` 需列出精确主机名；`full` 需 Lead 书面批准）
- [ ] 代理策略文件已计算 sha256 并写入批次记录（`policy_sha256`）
- [ ] 全局黑名单包含：自家仓库、榜单、题库、对象存储、paste/Gist 站
- [ ] 包镜像为只读（publish/上传类请求 403）；搜索代理黑名单生效
- [ ] 出口代理日志（`dns.jsonl`/`url.jsonl`）的采集与归档路径可用
- [ ] 沙箱 provider 是否支持所请求网络模式已核实（Docker/Podman 的 `no-network`/`allowlist` 依赖 nftables egress-control sidecar；不支持时 Harbor 会在 validation 阶段拒绝）

~~~bash
# 代理策略快照与哈希（控制面执行）
sha256sum /etc/egress/policy.yaml | tee batches/2026-11-b1/egress-policy.sha256
grep -E 'blocklist|allowlist' /etc/egress/policy.yaml | head -40
~~~

### A3. 资源与宿主/设备

- [ ] Linux 宿主：CPU/内存/磁盘余量满足并发公式（[01-architecture](../../01-architecture.md) §6）
- [ ] macOS 宿主：Tart 金镜像可克隆；**每台物理 Mac 最多 2 个并发 macOS VM**，槽位计数正确
- [ ] GPU 节点：显存/算力配额可分配；每 GPU 并发 1–2 路
- [ ] 鸿蒙设备池：`hdc list targets` 全部在线；设备系统版本、基线包列表、基线哈希已登记
- [ ] 性能机：独占可用；临时用户创建/删除流程演练通过；温度/负载采集可用
- [ ] 预热池：池内实例均为**从未跑过 trial** 的新实例（抽查创建时间与 trial 历史）
- [ ] 宿主健康探测与自动摘除策略已启用（心跳 60 s，连续 2 次失联摘除）

### A4. 密钥与访问

- [ ] 模型 API key、provider key、SSH 私钥只在控制面/网关；题包与镜像中无任何凭证
- [ ] per-trial key 租约机制可用（网关侧签发/回收）
- [ ] 网关预算上限与供应商锁定（响应头版本校验）已配置
- [ ] 密钥扫描脚本与脱敏规则已就绪（env/文件/history/日志）
- [ ] 控制面 SSH/ACP 为单向进入（沙箱不能主动连控制面除代理外地址）

### A5. 审计与证据

- [ ] 证据包目录约定可用：`<trial-dir>/isolation/`（preflight/artifact/canary/net/device/judge）
- [ ] 事件流必需事件类型齐全（[02-eval-flow](../../02-eval-flow.md) §11），`canary.injected`/`preflight.result`/`canary.scanned` 可写入
- [ ] 金丝雀登记表可写、可按 token 反查 trial
- [ ] 本期体检负责人（auditor）已指定；报告模板 [isolation-audit-report-template.md](isolation-audit-report-template.md) 已就绪

## B. 每个 trial 启动前检查（I1–I9 快速版）

在 `pending → leased → running` 之间执行；I1–I9 必须全部 pass，证据写 `trial.isolation.checks[]`。

| # | 检查 | 命令/证据 | 通过标准 |
| --- | --- | --- | --- |
| I1 | 沙箱/设备租约独占 | provider 回执 + 批次 lease 唯一性查询 | 本 trial 的 lease 全局唯一；前一租约已销毁 |
| I2 | 根文件系统只读 | `findmnt -no OPTIONS /`；`touch /.__ro_probe` | 含 `ro`；写探测失败 |
| I3 | /tmp、$HOME、工作区私有 | `findmnt -no FSTYPE /tmp`；跨 trial marker 探测 | tmpfs/overlay；marker 不可见 |
| I4 | 私有 netns + 默认断网 | `readlink /proc/self/ns/net`；`curl --max-time 3 https://example.com` | netns 与宿主不同；请求失败（或仅代理可达） |
| I5 | 无宿主可写挂载 / 无 docker socket / 无特权 | `findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS`；`test ! -S /var/run/docker.sock` | 无 rw 宿主路径；socket 不存在；无 privileged/cap_add |
| I6 | **显式**非 root + 无 sudo | `id -u`；`sudo -n true`；`capsh --print` | uid≠0；sudo 失败；无 cap_sys_admin |
| I7 | 资源配额 | `cat /sys/fs/cgroup/{cpu.max,memory.max,pids.max}` | 与 task.yaml 资源一致 |
| I8 | 身份去标识 | `pwd`；`env` 扫描；hostname | 路径随机；无 TASK_ID/MODEL/BATCH/EVAL/HARBOR；主机名中性 |
| I9 | 镜像 digest + 金丝雀注入 + 无凭证 | digest 文件；埋点 sha256；密钥扫描 | digest 匹配；全部埋点哈希一致；无 `sk-`/私钥 |

~~~bash
# 预检结果聚合断言（控制面执行，数据来自沙箱探针 stdout）
python3 - "$TRIAL_DIR/isolation/preflight.json" <<'PY'
import json, sys
rows = [line.split("\t") for line in open(sys.argv[1]) if line.strip()]
got = {r[0]: r for r in rows}
for i in range(2, 10):
    k = "I%d" % i
    assert got.get(k, ["", "fail"])[1] == "pass", "%s 预检未通过: %r" % (k, got.get(k))
assert "uid=0" not in got["I6"][2], "Agent 是 root"
print("I1–I9 预检通过")
PY
~~~

## C. 不通过时的处置

| 情况 | 处置 |
| --- | --- |
| I2/I3/I5/I6 失败 | 释放租约 → 换宿主重建 → 重跑预检（≤3 次）；镜像/配置有缺陷则退回题目包 |
| I4 失败（能出网） | 立即停止派发；按安全事件处理（04-isolation §14），禁止"先跑再查" |
| I7 配额缺失 | 补配额后重建；不得在无配额沙箱上跑 |
| I8 身份泄露 | 修 provisioner（随机路径/中性名）后重建 |
| I9 digest 不符 / 埋点缺失 / 密钥命中 | trial 作废；密钥命中按 S1 事故处理并轮换凭证 |
| 同一宿主连续 3 次预检失败 | 宿主摘除，进 incident 复盘 |

## D. 签字

| 项 | 结论（PASS/FAIL/WAIVER） | 证据 URI | 负责人 | 时间 |
| --- | --- | --- | --- | --- |
| 批次期前（A1–A5） |  |  |  |  |
| 逐 trial 预检（I1–I9） |  |  |  |  |
| 豁免清单 |  |  | Lead 批准 |  |
