# 订单服务：把 API.md 的契约补齐

我们组有一个订单服务的小工程（当前目录下的 **ordersvc/**，Python 标准库，无第三方依赖）。它现在只会最基本的"下单 / 查单"，离上线还差一截：**API.md** 是我们和客户端约定好的契约，里面写的鉴权、限流、幂等、库存故障降级和 health 探测都还没做。

请按 **API.md** 把服务实现补齐。要求：

1. 以 **API.md 为唯一契约**：路径、请求头、状态码、error.code、Retry-After、响应头都按它来；不要修改 API.md。
2. 不要修改 **tests/** 下的文件，也不要为了通过而改测试（验收会核对测试文件哈希）。
3. 只用 Python 标准库（沙箱无网络），不要引入第三方框架。
4. 保持服务能用下面的命令启动、并按要求向 stdout 打印监听行：

       python3 -m ordersvc --port 0 --state-dir /tmp/orders --inventory-url http://127.0.0.1:9000 \
           --auth-tokens tok-a,tok-b --rate-limit 5/10

5. 订单和幂等记录必须落盘，重启不丢；库存服务挂掉时服务要降级而不是崩。
6. 自测提示（本地库存桩已经放在 tools/ 下）：

       python3 tools/inventory_stub.py --port 9000 &
       python3 -m unittest discover -s tests -v

完成后说明你补了哪些能力、关键取舍是什么。
