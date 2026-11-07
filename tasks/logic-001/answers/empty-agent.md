# 空 Agent 对照：logic-001

## 1. 操作

空 Agent 不执行任何动作：不读记录、不写答案文件、不回复解题内容（模拟「模型完全没有产出」）。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 提交产物 | 不存在 `/workspace/answer.json` | 无操作 |
| HG1 答案正确 | false | 无答案 → `ANSWER_MISSING` |
| HG2 格式合规 | false | 无答案 → 格式违规 `ANSWER_MISSING` |
| HG3 输入未篡改 | true | 未碰输入（这是唯一可能为真的门槛） |
| 硬门槛总判定 | **Failed（F）** | 存在 required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛失败直接短路，不进入 L2/L3 |
| 轮次 | 第 1 轮观察即判 Failed；控制器不发反馈（无可反馈产物） | 停止条件：硬门槛失败 |

## 3. 本地复现

```bash
cd llm-eval/appendix/examples/logic-001
python3 hidden-tests/verify_submission.py --answer /tmp/does-not-exist.json; echo "exit=$?"
# 期望输出：HG1_answer_correct=False HG2_format_ok=False HG3_input_untampered=True
# 期望 exit=1
```

## 4. 为什么这个对照是必要的

如果空 Agent 也能拿到 P/A，说明硬门槛没有真正拦截、或答案比对把「缺答案」当成了「无缺陷」。本包用 `ANSWER_MISSING` 违规码显式区分「没交」与「交对了」，空 Agent 必须落在 Failed。

## 5. 记录格式

本包对空 Agent 的验收记录应写入 trial 记录：`result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: false}, {id: HG3, pass: true}]`、`result.grades[].tier='F'`、`validity='valid'`（成绩有效，模型自身失败）。
