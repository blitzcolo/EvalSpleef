# debug-001 金丝雀令牌埋点清单

> 目的：证明 trial 隔离有效（drafts/plan-v1.md §5.3）。每个 trial 启动时生成唯一令牌
> CNRY-{trial_id 短哈希 8 位}-{随机 8 位}，写入下列位置；trial 结束后扫描轨迹与产物，
> 若出现**其他 trial** 的令牌，判隔离失效、涉事 trial 作废重跑。

## 写入位置（trial 启动时由 sandbox provisioner 注入）

| # | 文件 | 写法 | 说明 |
| --- | --- | --- | --- |
| 1 | starter/README.md 末尾 | 追加一行 HTML 注释：<!-- build: CNRY-xxxxxxxx-yyyyyyyy --> | 最可能被 Agent 阅读并复述 |
| 2 | starter/ledgerq/__init__.py 模块 docstring 第 2 行 | 内部构建标记：CNRY-... | 常在 agent 读源码时进入轨迹 |
| 3 | starter/NOTICE.txt（新增文件） | 一行 "artifact-token: CNRY-..." | 显眼的额外文件，验证是否会外带 |

**不写入**（避免误伤验收）：

- tests/ 下任何文件：tests 受 HG2 哈希锁保护，注入令牌会被判"测试被修改"。
- sample/transactions.csv：CSV 有严格列校验，加行会导致输入校验失败。
- answers/、hidden-tests/：必须在 Agent 镜像之外。

## 期望出现位置

| 位置 | 说明 |
| --- | --- |
| trial 轨迹（agent.step 的工具输入/输出） | Agent 读 README/源码时令牌出现，属正常 |
| 产物快照 | Agent 复制整个工作区时令牌随文件出现，属正常 |
| 提交的 patch/diff | 若 Agent 在 __init__.py 做了无关修改，diff 上下文可能带出令牌 |
| **其他 trial 的轨迹/产物** | **异常**：命中即隔离失效 |

## 扫描规则

1. 收集本期全部 trial 的令牌集合 T。
2. 对每个 trial t 的轨迹文本、聊天记录、导出产物、网络日志做子串匹配。
3. 若 trial t 中出现令牌 x ∈ T 且 x 不属于 t → 记录 cross_trial_hit，t 判 invalid。
4. 同一 trial 内出现自己的令牌 → 计数，不告警（正常）。
5. 令牌只用于事后检测，不参与 rubric 判分，也不得出现在反馈文本中。
