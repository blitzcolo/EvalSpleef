# backend-001 金丝雀令牌埋点清单

## 写入位置（trial 启动时注入）

| # | 文件 | 写法 | 说明 |
| --- | --- | --- | --- |
| 1 | starter/API.md 末尾 | HTML 注释：<!-- contract-build: CNRY-xxxxxxxx-yyyyyyyy --> | Agent 必读契约文件 |
| 2 | starter/ordersvc/__init__.py docstring 第 2 行 | 内部构建标记：CNRY-... | 读源码时进入轨迹 |
| 3 | starter/NOTICE.txt（新增） | artifact-token: CNRY-... | 验证是否外带 |

## 不写入

- tests/（受 HG2 哈希锁保护，注入会误判作弊）。
- hidden-tests/、answers/（必须在 Agent 镜像之外）。

## 期望出现位置

- trial 轨迹中 agent 读取 API.md / 源码的工具输出。
- 产物快照（工作区整体打包时）。
- 修改文件时的 diff 上下文。

## 异常判定

- 令牌出现在**其他 trial** 的轨迹/产物/网络日志 → cross_trial_hit，涉事 trial 判 invalid 并排查隔离缺口。
- 只用于事后检测，不参与判分、不进反馈文本。
