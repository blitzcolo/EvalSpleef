# 空 Agent 对照：data-001

## 1. 操作

空 Agent 不执行任何动作：不读日志、不写脚本、不回复内容。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 提交产物 | 不存在 `/workspace/clean_logs.py` | 无操作 |
| HG1 样例报告 | false | 脚本缺失 → `SCRIPT_FAILED` |
| HG2 隐藏日志 | false | 脚本缺失 |
| HG3 完整性 | true（输入未改动） | 唯一可能为真的门槛 |
| 硬门槛总判定 | **Failed（F）** | required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛短路 |

## 3. 本地复现

```bash
cd tasks/data-001
python3 hidden-tests/verify_submission.py --submission /tmp/does-not-exist.py; echo "exit=$?"
# 期望：HG1_sample_ok=False HG2_hidden_ok=False HG3_integrity_ok=True，exit=1
```

## 4. 记录格式

`result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: false}, {id: HG3, pass: true}]`、`result.grades[].tier='F'`、`validity='valid'`、`flags=["budget_exhausted"]`（若为预算耗尽场景）。