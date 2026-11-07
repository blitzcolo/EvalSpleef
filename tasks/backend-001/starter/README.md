# ordersvc

订单服务小工程（Python 标准库，无第三方依赖）。当前是早期骨架：订单创建/查询与 JSON 落盘已可用，
但 **API.md 里的鉴权、限流、幂等、库存故障降级与 health 探测还没有实现**。请按 API.md 补全。

## 运行

    python3 -m ordersvc --port 0 --state-dir /tmp/orders --inventory-url http://127.0.0.1:9000 \
        --auth-tokens tok-a,tok-b --rate-limit 5/10

## 本地库存桩

    python3 tools/inventory_stub.py --port 9000

## 测试

    python3 -m unittest discover -s tests -v

## 目录

    ordersvc/
      server.py     HTTP 路由与处理
      store.py      订单/幂等记录持久化（JSON 文件）
      inventory.py  库存依赖客户端
    tools/inventory_stub.py   本地库存桩（仅开发用）
    tests/test_smoke.py       现有冒烟测试
