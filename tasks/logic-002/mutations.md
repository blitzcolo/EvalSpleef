# logic-002 变异测试清单

要求 ≥5 个变异体被杀死（[docs/06-answer-and-acceptance.md](../../docs/06-answer-and-acceptance.md) 第 5 节）。执行：`bash hidden-tests/run.sh` 中 `test_t07_mutants_killed`。

| 编号 | 变异操作 | 期望被杀 | 杀死断言 |
| --- | --- | --- | --- |
| M1 | 删除实例 2 holes 的最后一个元素 | 是 | `HOLES_MISMATCH` |
| M2 | 实例 3 holes 追加 33 | 是 | `HOLES_OUT_OF_RANGE` |
| M3 | 实例 1 holes 反转（降序） | 是 | `HOLES_UNSORTED` |
| M4 | 替换实例 2 holes 的第一个元素 | 是 | `HOLES_MISMATCH` |
| M5 | 删除实例 4 derivation | 是 | `DERIVATION_MISSING` |
| M6 | 实例 3 id 改成 42 | 是 | `INSTANCE_ID_UNKNOWN` |
| M7 | 删除最后一组实例 | 是 | `INSTANCE_MISSING` |
| M8 | 外层包一段中文说明 | 是 | `NOT_PURE_JSON` |
| M9 | holes 出现重复编号 | 是 | `HOLES_DUPLICATED` |
| M10 | 推导塞代码块 / 超过 300 字 | 是 | `DERIVATION_HAS_CODE_FENCE` / `DERIVATION_TOO_LONG` |
| M11 | 空文件 / 不存在路径 | 是 | `ANSWER_MISSING` |

## 变异分数

- 一阶变异体 11 个：杀死率 11/11。
- 生成器/checker 自身被改：由 T09 哈希锁拦截并转人工；不依赖运行期测试。