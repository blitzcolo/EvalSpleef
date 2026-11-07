# 空 Agent 对照：harmony-001

## 1. 操作

空 Agent 不执行任何动作：不读工程、不改代码、不构建。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 交付产物 | 无 HAP，工作区代码未变 | 无操作 |
| HG1 构建/装机 | false | 没有产物 |
| HG2 隐藏验收（静态 + 数值） | false（静态检查缺实现、数值测试失败） | starter 里 image_ops.cpp 是空壳 |
| HG3 契约文件 | true（未被修改） | 唯一可能为真的门槛 |
| 硬门槛总判定 | **Failed（F）** | required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛短路 |

## 3. 本地复现

~~~bash
cd tasks/harmony-001
bash hidden-tests/run.sh --selftest        # 证明 starter 必然失败、参考解必然通过
bash hidden-tests/run.sh starter; echo "exit=$?"   # 期望非 0
~~~

## 4. 记录格式

result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: false}, {id: HG3, pass: true}]、
result.grades[].tier='F'、validity='valid'、flags=["budget_exhausted"]（若为预算耗尽场景）。
