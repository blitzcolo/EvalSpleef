# logic-001 变异测试清单

要求：≥5 个变异体必须被隐藏测试杀死（[docs/06-answer-and-acceptance.md](../../docs/06-answer-and-acceptance.md) 第 5 节）。执行方式：`bash hidden-tests/run.sh` 中的 `test_t07_mutants_killed`；变异体由隐藏测试在临时目录构造，不写入题包。

| 编号 | 变异对象 | 变异操作 | 期望被杀 | 杀死它的断言 |
| --- | --- | --- | --- | --- |
| M1 | 答案 next | 实例 3 的 next 第 0 位 +1 | 是 | `NEXT_MISMATCH` → HG1 fail |
| M2 | 答案 rule.perm | 实例 2 的 perm 改成 rot_r1 | 是 | `RULE_MISMATCH` → HG1 fail |
| M3 | 答案 rule.a | 实例 1 的 a 改成 3 | 是 | `RULE_MISMATCH` → HG1 fail |
| M4 | 答案 derivation | 删除实例 4 的 derivation | 是 | `DERIVATION_MISSING` → HG2 fail |
| M5 | 答案实例集合 | 删除最后一个实例 | 是 | `INSTANCE_MISSING` → HG1/HG2 fail |
| M6 | 答案 id | 实例 5 的 id 改成 99 | 是 | `INSTANCE_ID_UNKNOWN` → HG2 fail |
| M7 | 答案实例集合 | 追加一份实例 1 副本 | 是 | `INSTANCE_ID_DUPLICATED` → HG2 fail |
| M8 | 外层格式 | 在 JSON 前加一行中文说明 | 是 | `NOT_PURE_JSON` → HG2 fail |
| M9 | 推导文本 | 推导塞入三反引号代码块 | 是 | `DERIVATION_HAS_CODE_FENCE` → HG2 fail |
| M10 | 推导长度 | 推导重复到 800 字以上 | 是 | `DERIVATION_TOO_LONG` → HG2 fail |
| M11 | 提交文件 | 空文件 / 不存在的路径 | 是 | `ANSWER_MISSING` → HG1/HG2 fail |
| M12 | 参考解自洽 | 把参考解 A 的 rule 换成相邻候选 | 是 | `RULE_NOT_SELF_CONSISTENT` → HG1 fail |

## 未覆盖的变异（已知边界）

- 对 `generator.py` 的变异（如放宽唯一性条件）不会被运行期测试杀死，而由哈希锁（`hashes.json`，T09）拦截；哈希锁只能证明「这份测试文件没被改」，不能证明「生成器逻辑正确」，后者靠代码评审与 T01/T08 的种子抽样覆盖。
- 对 checker 本身的变异同理；因此 checker 的关键分支（格式、枚举范围、自洽）都写进了 T10/T07，并有参考解正向对照。

## 变异分数

- 一阶变异体（上表 12 个）：杀死率 12/12 = 1.00。
- 隐藏测试自身被改：T09 哈希锁失败 → 验收装置 invalid，转人工。
