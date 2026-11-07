# ordersvc API 契约（v1）

所有请求/响应均为 JSON（UTF-8）。错误响应统一形状：

    {"error": {"code": "<稳定错误码>", "message": "<人类可读说明>"}}

## 启动约定

    python3 -m ordersvc --port 0 --state-dir DIR --inventory-url URL \
        --auth-tokens tok-a,tok-b --rate-limit 5/10

- **--port 0** 表示由系统分配端口；进程启动后必须向 stdout 打印一行并 flush：

      ORDERSVC_LISTENING <实际端口>

- --state-dir：订单与幂等记录的持久化目录（重启后必须恢复）。
- --inventory-url：库存服务基地址（http://host:port）。
- --auth-tokens：逗号分隔的合法 Bearer token 集合。
- --rate-limit：**每 token** 的限流配置，格式 N/W，W 单位为秒，缺省 5/10。

环境变量等价（命令行优先）：ORDERSVC_PORT / ORDERSVC_STATE_DIR /
ORDERSVC_INVENTORY_URL / ORDERSVC_AUTH_TOKENS / ORDERSVC_RATE_LIMIT。

## GET /health（无需鉴权）

- 200：{"status": "ok", "inventory": "ok"}   库存依赖可用
- 200：{"status": "degraded", "inventory": "unavailable"}   库存依赖不可用
- 必须真实探测库存依赖（例如探测 GET {inventory-url}/health），不得写死。

## POST /orders

请求头：

| 头 | 必填 | 说明 |
| --- | --- | --- |
| Authorization | 是 | Bearer <token>，token 必须在 --auth-tokens 集合内 |
| Idempotency-Key | 是 | 1..128 可见 ASCII 字符；同一 key 的重复请求必须幂等 |
| Content-Type | 是 | application/json |

请求体：{"sku": "<非空字符串>", "quantity": <整数，1..99>}

处理顺序（严格）：鉴权 → 限流 → 请求体校验 → 幂等判重 → 库存校验 → 落盘 → 201。

响应与错误码：

| 状态 | error.code | 触发条件 |
| --- | --- | --- |
| 201 | — | 创建成功：{"order_id":"ord-<12位小写hex>","sku":"...","quantity":2,"total_cents":1000,"status":"created"} |
| 400 | invalid_request | JSON 解析失败 / 字段缺失 / 类型或范围不合法 / 缺少 Idempotency-Key |
| 401 | unauthorized | 缺少或非法 Bearer token |
| 409 | idempotency_conflict | 同一 Idempotency-Key 携带了不同请求体 |
| 422 | out_of_stock | 库存服务返回可用量不足或 SKU 不存在 |
| 429 | rate_limited | 超过 --rate-limit；响应带 Retry-After: 10 |
| 503 | inventory_unavailable | 库存服务不可用或 5xx；响应带 Retry-After: 5 |

幂等语义：

1. 同一 Idempotency-Key + 相同请求体（sku/quantity 规范化后相同）→ 返回首次的 201 响应原文，
   并带响应头 **Idempotent-Replayed: true**；不得创建第二个订单。
2. 同一 Idempotency-Key + 不同请求体 → 409。
3. 幂等记录必须落盘（重启后仍生效），且只对创建成功的订单生效；失败响应不影响重试。

## GET /orders/{order_id}

请求头：Authorization 必填。

- 200：与创建时相同的订单对象
- 401 unauthorized / 404 not_found（{"error":{"code":"not_found"}}）
- 429 rate_limited（限流与 POST 共用同一 token 配额）

## 库存依赖协议

    GET {inventory-url}/stock/{sku}

- 200：{"sku": "A-1", "price_cents": 500, "available": 10}
- 404：SKU 不存在（本服务映射为 422 out_of_stock）
- 5xx / 连接失败 / 超时（>1s）：重试至多 2 次后返回 503 inventory_unavailable

订单金额 total_cents = price_cents × quantity（整数分）。订单 ID 全局唯一。

## 限流细节

- 粒度：每个 Bearer token 独立计数；固定窗口 W 秒，窗口内第 N+1 个请求起返回 429。
- 与业务结果无关：400/401/422 也消耗配额；401（无 token）不消耗。
- 429 必须带 Retry-After: 10（秒）。
