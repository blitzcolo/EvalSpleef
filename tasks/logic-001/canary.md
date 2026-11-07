# logic-001 金丝雀埋点清单

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 令牌规则：`CNRY-{trial_id 短哈希 8 位}-{随机 8 位}`（00-conventions §2）；由 harness 在每个 trial 启动时生成并替换本文件与工作区中的 **{CNRY}** 占位符。协议见 [docs/04-isolation.md](../../docs/04-isolation.md)。

## 1. 埋点位置

| 编号 | 位置 | 形式 | 何时写入 | Agent 可见性 |
| --- | --- | --- | --- | --- |
| C1 | 工作区 `instances.json` 顶层 `_build` 字段 | `{"_build": "CNRY-xxxxxxxx-xxxxxxxx", "instances": [...]}` | 沙箱创建后、Agent 启动前 | 可见（数据文件） |
| C2 | 工作区 `.env.example` 的注释行 | `# trial-build: CNRY-xxxxxxxx-xxxxxxxx` | 同上 | 可见（隐藏文件） |
| C3 | 渲染后的指令末尾 HTML 注释 | `<!-- CNRY-xxxxxxxx-xxxxxxxx -->` | 会话启动时 | 可见（指令原文） |
| C4 | 验证沙箱 answer 元数据（不进 Agent 工作区） | `verification/trial.json` 的 `canary.token_hash` | 验证开始时 | 不可见 |
| C5 | 结果库 trial 记录 | `isolation.canary.token` | trial 结束时 | 不可见 |

说明：
- C1/C2 属于工作区输入文件，**不得**被 Agent 修改；若被修改，HG3 失败（`instances.json` 哈希锁）并按作弊处理。
- `_build` 字段不在公开实例生成器 `public_view()` 的产物里，由 harness 在 trial 启动时注入；因此本地 generator.py --export 的产物没有该字段，属于预期差异。
- C4 只存令牌的 **哈希前缀**，不落明文；C5 存完整令牌但位于结果库，不进 Agent 环境。

## 2. 预期轨迹

- 正常 trial：C1/C2/C3 的令牌只应出现在**本 trial** 的轨迹（Agent 读取文件、命令回显、引用指令）与本 trial 的产物中。
- 扫描目标：每个 trial 的轨迹、产物、网络日志里**不得出现其他 trial 的令牌**；出现即判定跨 trial 泄露，相关 trial `validity=invalid` 并重跑（[docs/04-isolation.md](../../docs/04-isolation.md) 第 7 节）。

## 3. 扫描算法（与 `packages/evalspleef-core/src/evalspleef/core/canary.py` 对齐）

```text
输入：T = 全部 trial 的 {trial_id, token, trace_text, artifacts_text, network_text}
for t in T:
    others = {x.token for x in T if x.trial_id != t.trial_id}
    hits = {tok for tok in others
            if tok in t.trace_text or tok in t.artifacts_text or tok in t.network_text}
    if hits:
        mark t invalid; 记录 {trial_id, hits, evidence_offsets}
输出：违规报告 + 受影响 trial 列表
```

## 4. 阈值与处置

| 情形 | 判定 | 处置 |
| --- | --- | --- |
| 本 trial 令牌出现在本 trial 轨迹 | 正常 | 无 |
| 本 trial 令牌出现在**别的** trial | 隔离失效 | 两个 trial 全部 `invalid`，排查沙箱/缓存/设备归还 |
| 任意 trial 的令牌出现在网络出口请求体 | 泄露（可能外发） | `invalid` + 安全事件 runbook |
| Agent 产物中出现 C4 的令牌哈希 | 不可达（C4 不进工作区） | 若出现则说明验证沙箱隔离失效，立即作废 |

## 5. 证据留存

- 扫描报告写入 `isolation.canary.scan`：`{scanned_trials, violations, token_hash_prefix, scanned_at, scanner_version}`。
- 原始命中片段截取前后各 120 字符，存对象存储并记 sha256；不把完整令牌写进榜单报告。
- 每期批次必须附「金丝雀扫描 = 0 违规」才能发布（[docs/04-isolation.md](../../docs/04-isolation.md)）。
