# Playbook · 无提示修复（Debug）

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../00-conventions.md) · 相关：[02-eval-flow](../../02-eval-flow.md)、[04-isolation](../../04-isolation.md)、[05-task-library](../../05-task-library.md)

面向赛道：debug（注入缺陷的真实小工程、无提示修复）。示例题包：[debug-001](../../../tasks/debug-001/task.yaml)。

## 1. 环境镜像要点

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 基础镜像 | 按工程语言：python:3.12-slim / node:22-slim / rust:1.8x-slim | 依赖预装进镜像，agent 沙箱无网络 |
| 工具 | python3 -m unittest、patch、diff、sha256（python 标准库实现） | 不装 pytest/jsonschema，验收脚本零第三方依赖 |
| 工作区 | starter/ 原样拷入 /workspace，tests/ 受哈希锁保护 | 测试文件哈希清单只在验证侧 |
| 资源 | 1-2 vCPU / 1-2 GiB / 2 GiB 磁盘 | 题包小，瓶颈在观察与多轮反馈 |
| 网络 | 断网 | 防止搜到公开题解或上传答案 |

## 2. 构建与运行命令

     # 进入工程根目录后（题包自带 README 约定入口）
     python3 -m unittest discover -s tests -v
     python3 -m ledgerq summary --input sample/transactions.csv --output /tmp/summary.json
     # 参考解应用（验证侧自检用）
     cp -R starter /tmp/ref && (cd /tmp/ref && patch -p1 < answers/reference-a.patch)

Agent 可见命令与验证侧命令必须一致（同一入口、同一 Python 版本），避免「本地能过、验收跑法不同」。

## 3. 观察手段清单（供 Observer 直接引用）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| 测试结果 | 隐藏回归测试逐条结果 | bash hidden-tests/run.sh -v | 通过/失败条目 + 断言原文（F2） |
| 测试文件哈希 | sha256 清单比对 | python3 hidden-tests/check_locks.py | 退出码 + 差异清单 |
| 改动范围 | 与 starter 快照 diff | diff -ru starter candidate | diff 统计（文件数/行数） |
| 最小改动检查 | 变更文件清单与函数级 diff | git diff --no-index --stat | 文件清单 |
| 变异测试 | 重新注入缺陷看测试是否抓到 | python3 hidden-tests/mutate.py <fixed> | killed/survived 表 |
| 行为取证 | CLI 端到端输出与退出码 | python3 -m ledgerq ... ; echo $? | stdout/stderr/exit code |
| 异常信息 | 非法输入时的异常与行号 | 隐藏 fixture 驱动 | traceback 摘要（F2） |
| 判分幂等 | 同一产物连跑两次 | run.sh --selftest | 两次结果一致 |

## 4. 验收命令

     # 隐藏验收（退出码 0=通过；3=测试被修改）
     bash hidden-tests/run.sh
     # 自检：参考解 A/B 通过、starter 失败、哈希锁生效、变异全杀
     bash hidden-tests/run.sh --selftest
     # 参考解验证
     cp -R starter /tmp/ref && (cd /tmp/ref && patch -p1 < answers/reference-b.patch)
     bash hidden-tests/run.sh /tmp/ref

硬门槛建议：隐藏回归测试全过 + 测试文件哈希未变 + 原有可见测试仍通过。参考 [06-answer-and-acceptance](../../06-answer-and-acceptance.md)。

## 5. 隔离要点

1. starter 从镜像新建，每 trial 独立 workspace；tests/ 哈希清单不进 Agent 镜像。
2. 隐藏测试与参考解只在验证沙箱；Agent 无法访问 hidden-tests/ 目录。
3. Agent 改坏环境不恢复、按产物判分（model 错误，有效成绩）。
4. 金丝雀令牌不写入哈希锁文件（否则误判作弊）；位置见题包 canary.md。
5. 同一 trial 内测试文件禁止被写；验证前再算一次哈希，命中即判 0 并标 cheat。

## 6. 常见故障与处置

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 隐藏测试误报 | fixture 路径依赖 / 工作目录不同 | run.sh 统一设置 LEDGER_CANDIDATE，fixtures 用绝对路径 |
| 测试被改误判 | 镜像注入令牌写入 tests/ | 令牌排除 tests/；命中先核对 canary.md |
| 断言抖动 | 时间/随机/哈希顺序不稳定 | 固定种子、冻结时钟、排序后比较 |
| 参考解 patch 不应用 | 上下文漂移（starter 被改过） | patch --dry-run 先校验；失败即题目 bug（version 处置） |
| 变异体存活 | 测试覆盖不足 | 补隐藏用例；变异得分 <100% 的题不进库 |
| Agent 只改测试 | 测试文件被写 | 哈希锁拦截 + cheat 标记；不重试 |
