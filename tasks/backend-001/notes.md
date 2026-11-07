# backend-001 答案与验收说明

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../../docs/00-conventions.md) · 相关：[backend-service playbook](../../playbooks/backend-service.md)

## 1. 结构与运行

    backend-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md
    ├── starter/                 # Agent 可见工作区（骨架 + API.md 契约 + 本地库存桩）
    │   ├── API.md               # 契约（唯一规格来源，不得修改）
    │   ├── ordersvc/            # 骨架：basic CRUD + JSON 落盘；缺鉴权/限流/幂等/降级
    │   ├── tools/inventory_stub.py
    │   └── tests/test_smoke.py  # 哈希锁定
    ├── answers/{reference-a.patch, reference-b.patch, empty-agent.md}
    └── hidden-tests/
        ├── run.sh / check_locks.py / locks.sha256
        ├── inventory_stub.py    # 验收侧可启停库存桩（故障注入用）
        └── tests/test_api.py    # 19 条隐藏 HTTP API 测试

## 2. 环境与依赖

- Python 3.11+ 标准库；本地验证环境 python3 3.15.2 / macOS 27.2 arm64。
- 目标镜像 python:3.12-slim（digest 上线前固定）；沙箱无网络。
- 隐藏测试只用 unittest + urllib，不依赖 pytest/requests。

## 3. 验证命令

    # 隐藏验收（默认 starter，预期失败）
    bash hidden-tests/run.sh
    # 自检：参考解 A/B 通过、starter 失败、哈希锁生效
    bash hidden-tests/run.sh --selftest

## 4. 实测证据（2026-11-06，本机）

    backend-001 selftest
      [OK]   reference-a：隐藏 API 测试通过（预期通过）
      [OK]   reference-b：隐藏 API 测试通过（预期通过）
      [OK]   starter（未实现鉴权/限流/幂等/降级）：隐藏 API 测试失败（预期失败）
      [OK]   tests/ 被修改：哈希锁拦截（预期拦截）
    selftest: 4 通过 / 0 失败
    SELFTEST PASS

    直接跑隐藏测试：参考解 A OK（19 tests）；参考解 B OK（19 tests）；
    starter FAILED（failures=10）。隐藏套件单次约 12s（含限流窗口与重启用例）。

## 5. 两份参考解差异

| 维度 | A（249 行 patch） | B（520 行 patch） |
| --- | --- | --- |
| HTTP | ThreadingHTTPServer 同步 | asyncio.start_server 流式解析 |
| 存储 | JSON 文件（orders.json / idempotency.json） | SQLite（WAL，两张表） |
| 限流 | 固定窗口 dict 计数 | 固定窗口分桶 + hmac.compare_digest 鉴权 |
| 依赖调用 | urllib + 3 次重试 | urllib + asyncio.to_thread 包装重试 |

## 6. 已知容差与限制

- 限流窗口 10s、上限 5 次/token：隐藏测试在同一进程内短时连发；并发抖动不影响。
- 启动等待上限 20s；每次用例独立临时 state-dir，避免串扰。
- 故障恢复用例通过重启服务验证（不要求运行中自愈），契约只要求 health 真实探测 + 503 降级。
- 隐藏测试单次 ~12s；验收超时设置 600s，留 50 倍余量。

## 7. Schema 校准

task.yaml / rubric.yaml / defects.yaml 已按 schemas/task|rubric|defect.schema.json v1.0 编写
（节字段、枚举、required 均对齐）。若后续任务库 Schema 升版，由 tasks-b 重新校准。

## 8. TODO

- 无 TODO(run-on-host)：全部可本地验证。
- 上线前：固定 python:3.12-slim digest；把 inventory stub 声明为同 trial 依赖服务。

## 9. 观测手段（给 Observer/裁判）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| API 响应 | curl 抓状态码/头/体 | curl -si -X POST ... | HTTP 原文（F2） |
| 错误码一致性 | 负向用例遍历 | python3 -m unittest ... -k TestAuth | JSON 断言 |
| 服务日志 | 启动进程 stdout/stderr | 观察器收集 | 日志文件 |
| 限流行为 | 固定 token 连发 6 次 | hidden-tests/tests/test_api.py | 状态码序列 |
| 故障注入 | 停/起 inventory stub | TestFaultInjection | health JSON + 503 |
| 持久化 | 重启后 GET 原订单 | TestPersistence | 响应对比 |
| 判分幂等 | 参考解连跑两次 | bash hidden-tests/run.sh --selftest | 两次一致 |
