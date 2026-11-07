# 空 Agent 对照：vulkan-001

## 1. 操作

空 Agent 不执行任何动作：不读工程、不改代码、不构建、不截图。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 交付产物 | 无构建产物、无 out/scene.ppm | 无操作 |
| HG1 构建/验证层/截图 | false | 没有产物 |
| HG2 隐藏验收（静态 + CPU 数学） | false（main.cpp 有 TODO、shadow_math 是空壳） | starter 现状 |
| HG3 契约文件 | true（未被修改） | 唯一可能为真的门槛 |
| HG4 截图相似度 | false | 没有截图 |
| 硬门槛总判定 | **Failed（F）** | required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛短路 |

## 3. 本地复现

~~~bash
cd tasks/vulkan-001
bash hidden-tests/run.sh --selftest
bash hidden-tests/run.sh starter
~~~

## 4. 记录格式

result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: false}, {id: HG3, pass: true}, {id: HG4, pass: false}]、
result.grades[].tier='F'、validity='valid'、flags=["budget_exhausted"]（若为预算耗尽场景）。
