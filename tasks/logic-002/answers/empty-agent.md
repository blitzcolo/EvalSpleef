# 空 Agent 对照：logic-002

## 1. 操作

空 Agent 不执行任何动作：不读记录、不写答案、不回复解题内容。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 提交产物 | 不存在 `/workspace/answer.json` | 无操作 |
| HG1 孔洞正确 | false | 无答案 → `ANSWER_MISSING` |
| HG2 格式合规 | false | 无答案 → 格式违规 |
| HG3 输入未篡改 | true | 未碰输入 |
| 硬门槛总判定 | **Failed（F）** | 存在 required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛短路 |
| 轮次 | 第 1 轮观察即 Failed | 停止条件：硬门槛失败 |

## 3. 本地复现

```bash
cd tasks/logic-002
python3 hidden-tests/verify_submission.py --answer /tmp/does-not-exist.json; echo "exit=$?"
# 期望：HG1_holes_correct=False HG2_format_ok=False HG3_input_untampered=True，exit=1
```

## 4. 记录格式

`result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: false}, {id: HG3, pass: true}]`、`result.grades[].tier='F'`、`validity='valid'`。