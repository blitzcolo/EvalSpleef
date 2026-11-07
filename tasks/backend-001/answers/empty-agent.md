# backend-001 空 Agent 预期结果

空 Agent = 不做任何修改直接结束。

| 门槛/条目 | 结果 | 依据 |
| --- | --- | --- |
| HG1 隐藏 API 测试 | **FAIL** | 19 条中 10 条失败：鉴权、限流、幂等、故障降级全部缺失 |
| HG2 测试文件哈希 | PASS | 未修改 |
| HG3 可见冒烟测试 | PASS | starter 自带 3 条冒烟测试本来就过（它们带合法请求头） |
| R1 鉴权 | FAIL | 无 Authorization 仍返回 201 |
| R2 限流 | FAIL | 连续 6 次请求全部成功，无 429 |
| R3 幂等回放 | FAIL | 同 key 两次请求生成两个订单 |
| R4 幂等冲突 | FAIL | 同 key 不同 body 不返回 409 |
| R5 health 降级 | FAIL | 库存依赖挂掉 health 仍报 ok |
| R6 依赖故障 503 | FAIL | 库存依赖挂掉返回 500 internal_error |

## 判定

- 硬门槛 HG1 失败 → **Failed**，不进入软评分。
- 缺陷扣分记录：D1/D2/D3/D4/D5 各 functional=3，合计 15。
- 无任何成功轮次；F1 反馈后仍无操作则预算耗尽仍 Failed。
