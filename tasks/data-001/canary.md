# data-001 金丝雀埋点清单

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 令牌规则：`CNRY-{trial_id 短哈希 8 位}-{随机 8 位}`（00-conventions §2）；harness 在 trial 启动时生成并替换 **{CNRY}** 占位符。协议见 [docs/04-isolation.md](../../../docs/04-isolation.md)。

## 1. 埋点位置

| 编号 | 位置 | 形式 | 何时写入 | Agent 可见性 |
| --- | --- | --- | --- | --- |
| C1 | `workspace/access.log` 末尾轮转注释行 | `# build CNRY-... (do not edit)` | Agent 启动前 | 可见 |
| C2 | `workspace/.env.example` 注释 | `# trial-build: CNRY-...` | Agent 启动前 | 可见 |
| C3 | 渲染后指令末尾 HTML 注释 | `<!-- CNRY-... -->` | 会话启动 | 可见 |
| C4 | 验证侧 trial 元数据 | `canary.token_hash`（哈希前缀） | 验证开始 | 不可见 |
| C5 | 结果库 trial 记录 | `isolation.canary.token` | trial 结束 | 不可见 |

注意：C1 会让"有效行/脏行总数"多一行——该注释行按题面口径属于 `bad_format` 脏行，生成器与 oracle 已把它计入（样例与隐藏日志各有一行），Agent 不得删除；删除会改变报告并被 HG1/HG2 抓到。

## 2. 预期轨迹与扫描

- 正常 trial：C1/C2/C3 只出现在本 trial 的轨迹与产物；脚本被验证侧执行时，输入日志里的令牌会出现在进程读取轨迹中，属于正常。
- 扫描规则：任一 trial 的轨迹/产物/网络日志出现**其他 trial** 的令牌 → 相关 trial `validity=invalid` 并重跑。
- 特别检查：Agent 提交的脚本源码中不得出现任何 CNRY 令牌（否则视为把工作区数据外带进产物）。

```text
for t in trials:
    for other in trials where other.trial_id != t.trial_id:
        haystack = t.trace + t.artifacts + t.network
        if other.token in haystack: invalidate(t, other); record_offsets()
if any(token in submission_source for token in all_tokens): flag cheat
```

## 3. 证据留存

扫描报告：`{scanned_trials, violations, submission_token_hits, token_hash_prefix, scanned_at}`；命中片段截前后 120 字符并记 sha256。