# logic-002 答案与验收说明

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 相关：[docs/06-answer-and-acceptance.md](../../docs/06-answer-and-acceptance.md)

## 1. 题目形态

- 32 格纸条按 `folds`（L=左半向右折，R=右半向左折）反复整叠对折，折完后在折好的叠上扎穿 `punches` 列，求展开后哪些格子有洞（升序）。
- 4 组实例由 `hidden-tests/generator.py`（种子 20261106）生成：折数 m ∈ {2,3,4}，扎 1-2 列；每个打孔列恰好穿透 2^m 层，答案规模 = 2^m × 打孔数。
- 双求解器交叉校验：正向模拟（按列维护格子集合）与逆向重建（列号反向展开）必须一致，否则生成器拒绝该实例。
- 难度：3（预估，标定 pending）。难度来自「折叠方向导致列序反转」与多层合并，而不是计算量。

## 2. 文件与职责

| 文件 | 侧 | 职责 |
| --- | --- | --- |
| `task.yaml` | 题包 | 元数据、预算、硬门槛 |
| `instruction.md` | Agent | 用户口吻说明（含 4 格示例） |
| `persona.md` | 用户 Agent | F0-F3 话术、FAQ、禁止泄露清单 |
| `rubric.yaml` | 判定 | 10 条原子 rubric |
| `defects.yaml` | 判定 | F1/F2/F3 素材（覆盖 R1-R6、R9） |
| `workspace/instances.json` | Agent | 4 组折叠/打孔参数（哈希锁） |
| `answers/reference-a.py` | 验证 | 参考解 A：正向模拟 |
| `answers/reference-b.py` | 验证 | 参考解 B：逆向重建 |
| `answers/empty-agent.md` | 验证 | 空 Agent 预期 Failed |
| `hidden-tests/run.sh` | 验证 | 自检 + 可选提交验收 |
| `hidden-tests/generator.py` | 验证 | 生成器 + 双求解器 |
| `hidden-tests/checker.py` | 验证 | 答案比对（机器码） |
| `hidden-tests/verify_submission.py` | 验证 | 三硬门槛提交验收 |
| `hidden-tests/test_hidden.py` | 验证 | 11 条隐藏测试 |
| `hidden-tests/hashes.json` | 验证 | 测试/输入哈希锁 |

## 3. 复现命令（本地已实跑）

```bash
cd tasks/logic-002
python3 hidden-tests/generator.py --validate
python3 answers/reference-a.py workspace/instances.json /tmp/answer-a.json
python3 answers/reference-b.py workspace/instances.json /tmp/answer-b.json
python3 hidden-tests/checker.py --answer /tmp/answer-a.json
bash hidden-tests/run.sh
CANDIDATE_ANSWER=/tmp/answer-a.json bash hidden-tests/run.sh
```

实跑证据（2026-11-06）：generator 自检输出 `[OK] 生成器自检通过：4 个实例，双求解器一致`；`bash hidden-tests/run.sh` → `Ran 11 tests ... OK`，exit 0；带候选答案运行时三闸全 True，exit 0。

## 4. 判定口径

| 项 | 口径 | 容差 |
| --- | --- | --- |
| holes | 升序整数列表精确相等 | 无（离散唯一解，不设容差） |
| 排序/去重 | 必须升序且无重复 | 无 |
| 推导 | ≤300 字符、≤3 句、无代码块 | 无 |
| 推导质量 | R5/R6/R7/R8 裁判判定，k=5 取中位 | 一致率 <0.6 转人工 |

## 5. 参考解设计

| 参考解 | 思路 | 复杂度 |
| --- | --- | --- |
| A 正向模拟 | 32 个桶逐折合并，每折检查列数减半 | O(m·32) |
| B 逆向重建 | 从折后列号倒推 2^m 个原始列 | O(m·2^m) |

两解不共享代码；隐藏测试 T05 断言 holes 相同。

## 6. 已知限制

- 折叠语义只支持「整叠对折」，不支持非居中折；题面已写明，避免歧义。
- 实例 3/4 的答案规模为 16 个洞，刻意避免 32 个（平凡）。
- 若需提高难度，可增加「展开后洞的连通区域」或「折痕位置」，须 MAJOR 升版。

## 7. 版本

- 1.0.0（2026-11-06）：首版。改生成器语义 = MAJOR 重跑；改题面文案 = PATCH；改 rubric = MINOR 重判。