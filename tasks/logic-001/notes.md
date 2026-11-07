# logic-001 答案与验收说明

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 相关：[docs/06-answer-and-acceptance.md](../../../docs/06-answer-and-acceptance.md) · [docs/05-task-library.md](../../../docs/05-task-library.md)

## 1. 题目形态

- 领域：6 格数字棋盘；隐藏规则 `next[i] = (a·cur[perm[i]] + b) mod 10`，`perm` 取 8 种位置置换之一、`a∈{1,3,7,9}`、`b∈0..9`，规则族共 320 条。
- Agent 拿到 5 组「无解说」对局记录（`workspace/instances.json`），要输出 `answer.json`：每组的规则、下一棋盘、1-3 句可核验推导。
- 参数化生成：`hidden-tests/generator.py`（种子 20261106，可重放）；生成时强制「规则族内唯一解 + 记录为最小充分集」，因此答案唯一，可脚本比对。
- 难度：3（预估，标定 pending）。每条记录只展示 2 次转移即已唯一；难点在系统性排除 320 条候选并给出可复核推导，而非计算量。

## 2. 文件清单与职责

| 文件 | 侧 | 职责 |
| --- | --- | --- |
| `task.yaml` | 题包 | 元数据、预算、硬门槛、产物声明 |
| `instruction.md` | Agent | 真实用户口吻的任务说明（含规则族定义与答案格式） |
| `persona.md` | 用户 Agent | F0-F3 话术、FAQ、禁止泄露清单 |
| `rubric.yaml` | 判定 | 12 条原子 rubric（script + llm_judge） |
| `defects.yaml` | 判定 | F1/F2/F3 预写现象素材（覆盖 R1-R7） |
| `workspace/instances.json` | Agent | 5 组记录（受保护输入，哈希锁） |
| `answers/reference-a.py` | 验证 | 参考解 A：穷举 320 条候选 |
| `answers/reference-b.py` | 验证 | 参考解 B：同余方程约束传播 |
| `answers/empty-agent.md` | 验证 | 空 Agent 预期 Failed |
| `hidden-tests/run.sh` | 验证 | 入口：自检 + 可选提交验收 |
| `hidden-tests/generator.py` | 验证 | 参数化生成器 + 唯一性校验 |
| `hidden-tests/checker.py` | 验证 | 答案比对（机器码，无自然语言反馈） |
| `hidden-tests/verify_submission.py` | 验证 | 三个硬门槛的提交验收 |
| `hidden-tests/test_hidden.py` | 验证 | 11 条隐藏测试（含变异体猎杀、哈希锁） |
| `hidden-tests/hashes.json` | 验证 | 测试与输入文件的 sha256 锁 |

## 3. 如何复现（本地已实跑）

环境：Python 3.15.2，只用标准库（无需 pytest/jsonschema）。

```bash
cd llm-eval/appendix/examples/logic-001

# 1) 生成器自检：唯一性 / 最小充分记录 / 确定性
python3 hidden-tests/generator.py --validate

# 2) 参考解 A/B 各自生成答案，并与 checker 比对
python3 answers/reference-a.py workspace/instances.json /tmp/answer-a.json
python3 answers/reference-b.py workspace/instances.json /tmp/answer-b.json
python3 hidden-tests/checker.py --answer /tmp/answer-a.json
python3 hidden-tests/checker.py --answer /tmp/answer-b.json

# 3) 全量隐藏测试（含 7 个变异体、空 Agent、格式违规、哈希锁）
bash hidden-tests/run.sh

# 4) 按真实提交验收（三个硬门槛）
CANDIDATE_ANSWER=/tmp/answer-a.json bash hidden-tests/run.sh
```

实跑证据（2026-11-06，本机 Python 3.15.2 / macOS）：
- `python3 hidden-tests/generator.py --validate` → `[OK] 生成器自检通过：320 条规则族，5 个实例`
- `bash hidden-tests/run.sh` → `Ran 11 tests ... OK`，exit 0
- `CANDIDATE_ANSWER=<参考解产物> bash hidden-tests/run.sh` → `HG1_answer_correct=True HG2_format_ok=True HG3_input_untampered=True`，exit 0

## 4. 判定口径与容差

| 项 | 口径 | 容差 |
| --- | --- | --- |
| rule.perm | 字符串精确匹配 8 个合法名字 | 无 |
| rule.a / rule.b | 整数精确匹配 | 无 |
| next | 6 个整数逐位精确匹配 | 无 |
| 推导篇幅 | ≤300 字符、≤3 句、无三反引号代码块 | 无 |
| 推导质量 | R6/R7/R8/R9/R10 由裁判按 rubric 判定 | k=5 取中位；一致率 <0.6 转人工 |

> 逻辑题**不设数值容差**：唯一解是离散集合，模糊比较会放过猜对一半的答案。

## 5. 参考解设计说明

| 参考解 | 思路 | 关键点 | 运行时间 |
| --- | --- | --- | --- |
| A `reference-a.py` | 穷举搜索：320 条候选全代入 | 简单直接，证明「答案在规则族内唯一」 | < 0.05 s |
| B `reference-b.py` | 约束传播：位置对建立同余式，扩展欧几里得解 a、回代 b | 不遍历规则族；对每个置换只做代数求解 | < 0.05 s |

两份实现不共享任何代码（各自独立定义置换表与求解器）；隐藏测试 T05 断言两者答案一致，T07 断言 7 个变异体都被杀死，T09 断言测试文件与输入文件哈希不变。

## 6. 已知限制

- 规则族固定公开，强模型可写脚本穷举；本题定位是逻辑赛道入门/标定题，不追求高区分度。若要提高难度，可在 MINOR 版本改为双规则（P1/P2 各一条隐藏规则），需同步升 rubric 与生成器。
- 生成器当前 5 个实例全部为 2 次转移即唯一（m=2）；m=3 的最小充分记录在随机搜索中出现率低，生成器只在前 2 次不唯一时才采用。标定后若发现过易，可放宽 `MAX_STEPS_TRIED` 并重跑隐藏测试。
- 推导质量依赖裁判：R6/R7 为 functional，若裁判一致性 < 0.6 进人工队列（[docs/03-grading.md](../../../docs/03-grading.md)）。

## 7. 版本与重跑规则

- 1.0.0（2026-11-06）：首版上线。
- 修改 `generator.py` 的规则族/种子 = 验收语义变化 → MAJOR 升版并重跑全部 trial。
- 只改 instruction/persona 文案 = PATCH，可复用旧 trial；改 rubric 判定口径 = MINOR，需重判（[docs/05-task-library.md](../../../docs/05-task-library.md) 第 8 节）。
