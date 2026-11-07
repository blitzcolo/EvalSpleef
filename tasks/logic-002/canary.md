# logic-002 金丝雀埋点清单

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 令牌规则：`CNRY-{trial_id 短哈希 8 位}-{随机 8 位}`（00-conventions §2）；harness 在 trial 启动时生成并替换 **{CNRY}** 占位符。协议见 [docs/04-isolation.md](../../docs/04-isolation.md)。

## 1. 埋点位置

| 编号 | 位置 | 形式 | 何时写入 | Agent 可见性 |
| --- | --- | --- | --- | --- |
| C1 | `workspace/instances.json` 顶层 `_build` | `{"_build": "CNRY-...", "instances": [...]}` | Agent 启动前 | 可见 |
| C2 | `workspace/.env.example` 注释 | `# trial-build: CNRY-...` | Agent 启动前 | 可见 |
| C3 | 渲染后指令末尾 HTML 注释 | `<!-- CNRY-... -->` | 会话启动 | 可见 |
| C4 | 验证侧 answer 元数据 | `canary.token_hash`（只存哈希前缀） | 验证开始 | 不可见 |
| C5 | 结果库 trial 记录 | `isolation.canary.token` | trial 结束 | 不可见 |

## 2. 规则

- 正常 trial：C1/C2/C3 只应出现在本 trial 的轨迹与产物中。
- 扫描：任一 trial 的轨迹/产物/网络日志出现**其他 trial** 的令牌 → 相关 trial `validity=invalid` 并重跑（[docs/04-isolation.md](../../docs/04-isolation.md) 第 7 节）。
- 修改含 C1 的输入文件会同时触发 HG3 失败，按作弊处理。

## 3. 扫描算法

```text
for t in trials:
    for other in trials where other.trial_id != t.trial_id:
        if other.token in (t.trace + t.artifacts + t.network):
            invalidate(t); invalidate(other); record(t, other, offsets)
```

## 4. 证据留存

扫描报告字段：`{scanned_trials, violations, token_hash_prefix, scanned_at, scanner_version}`；命中片段截前后 120 字符并记 sha256。每期发布前必须附「0 违规」结论。