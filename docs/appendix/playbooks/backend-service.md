# Playbook · 后端服务

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../docs/00-conventions.md) · 相关：[02-eval-flow](../../docs/02-eval-flow.md)、[04-isolation](../../docs/04-isolation.md)、[05-task-library](../../docs/05-task-library.md)

面向赛道：backend（API 服务、鉴权、限流、幂等、依赖降级）。示例题包：[backend-001](../examples/backend-001/task.yaml)。

## 1. 环境镜像要点

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 基础镜像 | python:3.12-slim / node:22-slim / rust:1.8x-slim（按题目语言） | 按 digest 固定；语言版本进 batch 记录 |
| 依赖服务 | compose 起在**同 trial 私有网络**：postgres:16、redis:7 等 | 依赖版本固定 digest；不用宿主已有实例 |
| 客户端工具 | curl、jq、ss、python3-urllib | 观察器与故障注入需要 |
| 网络 | 默认断网（依赖服务走内网） | 需要联网的题只开包镜像白名单 |
| 资源 | 2 vCPU / 2-4 GiB；压测题 4 vCPU | 限流/压测对 CPU 抖动敏感，需独占配额 |

## 2. 构建与运行命令

     # 依赖服务（同 trial 网络，独立 project 名）
     docker compose -p "trial-$TRIAL_ID" up -d --wait
     # 应用（示例：backend-001 标准库服务）
     python3 -m ordersvc --port 0 --state-dir /tmp/state \
         --inventory-url http://127.0.0.1:9000 --auth-tokens tok-a,tok-b --rate-limit 5/10
     # 等待就绪（最多 30s，失败按 environment 处理）
     for i in $(seq 1 30); do curl -fsS http://127.0.0.1:$PORT/health && break; sleep 1; done

服务端口、依赖地址、token 全部通过参数/环境变量注入；**不写死端口**，避免同机 trial 冲突。

## 3. 观察手段清单（供 Observer 直接引用）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| API 响应 | curl 带 -i 抓状态码/头/体 | curl -si -X POST ... | HTTP 原文（F2 素材） |
| 错误码一致性 | 遍历错误场景比对 error.code | 隐藏测试的 negative cases | JSON 断言 |
| 服务日志 | compose logs / stdout 重定向 | docker compose -p <p> logs --no-color | 日志文件 |
| 限流行为 | 固定 token 连发 N 次 | python3 hidden-tests/tests/test_api.py | 状态码序列 |
| 故障注入 | 停依赖后调用接口 | docker compose stop <svc>；观察 health/503 | 响应 + health JSON |
| 恢复验证 | 重启依赖后再测 | docker compose start <svc> | 同上 |
| 数据库状态 | psql -c "select count(*) ..." | 只读查询 | 行数快照 |
| 压测 | 内置脚本 100-500 并发 | python3 scripts/load.py --p95 | p95/p99 毫秒 |

观察器必须区分「业务失败」（照常判分）与「基础设施失败」（无效重跑），口径见 [02-eval-flow](../../docs/02-eval-flow.md) 第 8 节与 [Harbor 错误分类](https://docs.harborframework.com/jobs/error-classification.md)。

## 4. 验收命令

     # 隐藏 API 测试（真实 HTTP，非 mock）
     bash hidden-tests/run.sh
     # 自检：参考解 A/B 通过、空 Agent 失败、测试哈希未变
     bash hidden-tests/run.sh --selftest
     # 有压测门槛时
     python3 hidden-tests/load.py --base http://127.0.0.1:$PORT --p95-ms 200

硬门槛建议：隐藏 API 测试全过、错误响应结构统一、故障注入后 health/降级符合契约、测试文件哈希未变。

## 5. 隔离要点

1. 依赖服务**同 trial 私有 compose 网络**；不同 trial 不复用数据库/缓存/端口。
2. 不挂 Docker socket、不开 privileged；Agent 不直接操作宿主 docker。
3. 每个 trial 独立 volume；teardown 用 docker compose down -v 并校验容器/网络/卷全部消失。
4. 故障注入只针对本 trial 的依赖实例，禁止动共享服务。
5. 压测在 trial 配额内执行；不与其他 trial 共享 CPU 配额，避免互相压测干扰。

## 6. 常见故障与处置

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 端口被占用 | 固定端口/上一 trial 残留 | 随机端口 + 启动健康检查；清理残留容器（environment/retry） |
| 依赖启动慢导致首轮失败 | 无 --wait / 无重试 | compose up --wait + 30s 轮询；仍失败判 environment |
| 连接被拒但依赖在跑 | 网络命名空间/地址写错 | 观察器检查 compose 网络与 URL 注入变量 |
| 限流用例偶发通过 | 窗口跨测例共享状态 | 每个用例独立服务实例与临时 state-dir |
| 压测 p95 抖动大 | 共享 CPU / 后台进程 | 独占配额 + 5 次取中位；排除宿主负载 |
| 残留卷污染下一轮 | teardown 不完整 | 校验 volume 列表为空；否则隔离体检记 incident |
