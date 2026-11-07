# examples · 参考实现样例

> 状态：v1 · 维护：impl · 最后更新：2026-11-06
> 上游：`../README.md` · 相关：`../../docs/00-conventions.md`、`../../schemas/`

本目录提供 CLI 可跑通的最小样例。7 份文档与 `llm-eval/schemas/` 对齐，由内置
`evalspleef.core.schema_lite` 在 `python3 -m evalspleef.core lint` 中逐个校验（7/7 通过）；
其余 3 份是 CLI 内部输入格式（没有对应 JSON Schema），lint 会显式跳过。

## 文件 ↔ Schema

| 文件 | 校验 Schema | 内容 |
| --- | --- | --- |
| `task-minimal/task.json` | `task.schema.json` | web-001 题包元数据（预算冻结字段、硬门槛、canary 埋点、隔离声明） |
| `task-minimal/rubric.json` | `rubric.schema.json` | 6 条原子 rubric（2 functional + 4 minor；f1_symptom 用户口吻） |
| `task-minimal/defect-catalog.json` | `defect.schema.json` | 缺陷现象目录（F1/F2/F3 预写素材） |
| `task-minimal/defect-observation.json` | `defect.schema.json` | 第 2 轮观察报告（缺陷跨轮状态、evidence、summary） |
| `task-minimal/feedback.json` | `feedback.schema.json` | 一条 F1 反馈记录（单缺陷、字段白名单、leak_check、budget 快照） |
| `task-minimal/grade.json` | `grade.schema.json` | 一次 B+ 判分（P6 G-F1-BPLUS，penalty=5，cost 拆分） |
| `task-minimal/trial.json` | `trial.schema.json` | 完整 trial 记录（rounds/isolation/result/timestamps/audit） |
| `grading-case.json` | （内部）evalspleef.core grade 输入 | V05 同构：1 个 functional 修复 + 2 个 minor 未修 → B+ |
| `canary-trials.json` | （内部）evalspleef.core scan 输入 | 3 个 trial，故意含跨 trial 令牌与外泄各 1 例 |
| `batch-2026-11-b1/batch-input.json` | （内部）evalspleef.core report 输入 | 5 个 cell：3 个已出分 + 1 Pass 继承 + 1 Skip |

## 用法

```bash
cd llm-eval/reference
python3 -m evalspleef.core lint                    # 7/7 通过 + 3 跳过
python3 -m evalspleef.core grade                   # 读 examples/grading-case.json
python3 -m evalspleef.core scan                    # 读 examples/canary-trials.json，退出码 1
python3 -m evalspleef.core report                  # 读 batch-input.json，输出 markdown
python3 -m evalspleef.core simulate --seed 7       # 端到端模拟，不读样例文件
```

说明：`batch-input.json` 中的 trial 记录是榜单聚合用的**精简视图**（只保留
trial_id/state/validity/grades），真实 trial 记录以 `task-minimal/trial.json` 为准；
判分数字为演示用，不代表任何真实模型成绩。
