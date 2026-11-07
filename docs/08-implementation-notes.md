# 08 · 参考实现说明（Implementation Notes）

> 状态：v1 · 维护：impl · 最后更新：2026-11-06
> 上游：`00-conventions.md` · 相关：`01-architecture.md`、`02-eval-flow.md`、`03-grading.md`、`../reference/README.md`、`../appendix/grading-vectors.json`

本文记录 `reference/evalkit` 的实现决策、与 Schema/规格的对齐方式、实跑证据、已知限制与后续 TODO。
可执行命令与模块映射见 `../reference/README.md`，本文不重复命令清单。

## 1. 实现范围

| 层 | 模块 | 状态 |
| --- | --- | --- |
| 数据契约 | `models.py`（枚举、Budget/Escalation、RubricSet、Defect、Grade、TrialRecord） | 完成，逐字对齐 Schema |
| 流程 | `state.py`（事件驱动状态机、P0-P5 终态、grade 版本、人工覆盖） | 完成，对齐 02 §4.2/§6/§8 |
| 判定 | `grading.py`、`judge.py`、`defects.py`、`feedback.py` | 完成，对齐 03 §6-§11 与 `grading-vectors.json` |
| 隔离 | `canary.py`、`adapters/harbor.py` | 完成，令牌格式/扫描/违规分类对齐 00 §2 与 trial schema |
| 输出 | `report.py`、`cli.py` | 完成，单元格格式对齐 00 §4.4、Cell 聚合对齐 02 §10/03 §9.9 |
| 校验 | `schema_lite.py` | 完成（draft 2020-12 子集，无第三方依赖） |

## 2. 关键实现决策

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | 判定用**纯函数**（`compute_grade(...) -> GradeOutcome`），不读写全局状态 | 满足 03 §9.8 I15「同一输入重放 10 次结果一致」；regrade 可重放 |
| D2 | 状态机与持久化记录分离：`TrialMachine` 持事件历史，`TrialRecord` 是纯数据 | 事件日志是权威（02 §11），Record 可随时由事件投影得到 |
| D3 | 反馈控制器**无状态**：输入 tracker+history+budget，输出唯一决策 | 03 §7.7 的伪代码可逐行转测试；重放/重判不依赖隐式状态 |
| D4 | Defect 只保留 Schema 字段；跨轮内部元数据（last_round/fixed_round/rounds_failed）直接进入 Defect（trial schema 的 observation_item 要求） | 避免重复存储与序列化污染；additionalProperties=false 可通过 |
| D5 | 反馈只针对未修复 functional/fatal；minor 永不触发反馈 | 03 §7.1 原则 4/§7.4；minor 只影响 P/A 与半档分子分母 |
| D6 | 半档用整数比较 `3 * need_feedback <= total_defects`，不引入浮点 | 03 §9.4；向量 V05/V07 边界（1/3） |
| D7 | need_feedback / multi_round_f1 按**已修复缺陷记录**计，而非按反馈轮次计 | 03 §9.1 的精确定义；避免"发了反馈但没修好"被算进半档分子 |
| D8 | 作弊/格式违规 penalty=0 但成绩 valid 且计入榜单（记 0） | 03 §9.6/§10.1 与 grade.schema.json 的 allOf 约束 |
| D9 | 不产出 grade 的情形单独返回 `GradeOutcome(grade=None)`：invalid→G-INVALID-00、manual/anomaly→无 grade 转人工 | 03 §9.2 P0/P3/P14；"不静默记 0" |
| D10 | 适配层按事件信封映射，不复制 Harbor 生命周期 | 01 §8「锁版本 + 薄适配」；升级只改 `adapters/` |
| D11 | Schema 校验器自研极简版（schema_lite） | 环境无 jsonschema；负责 required/enum/type/$ref/oneOf/if-then，足够拦住 examples 漂移 |
| D12 | 所有可执行证据由 `unittest` + CLI 双通道产生 | 测试证明规则；CLI 证明样例与命令可跑 |

## 3. 与 Schema 的校验方式

- 入口：`python3 -m evalkit lint` 扫描 `reference/examples/` 下 JSON，按内容推断对应 Schema：
  - 含 `document_type=observation|catalog` → `defect.schema.json`；
  - 含 `feedback_index + leak_check` → `feedback.schema.json`；
  - 含 `grade_version + decision` → `grade.schema.json`；
  - 含 `rubric_version + items + task_id` → `rubric.schema.json`；
  - 含 `state + grades` → `trial.schema.json`；含 `platform + budget + hard_gates` → `task.schema.json`。
- 跨文件 `$ref`（如 `defect.schema.json#/$defs/trial_id`、`task.schema.json#/properties/budget`）由
  `SchemaStore` 按同目录文件名解析；片段指针按 RFC 6901 反转义。
- 已覆盖关键字：`type/properties/required/additionalProperties/enum/const/oneOf/anyOf/allOf/if-then-else/not/items/contains/minItems/maxItems/uniqueItems/minimum/maximum/exclusive*/minLength/maxLength/pattern/format=date-time/$ref`。
- 未覆盖：`dependentSchemas/dependentRequired/propertyNames/unevaluatedProperties/prefixItems` 等；本仓库 Schema 目前未使用。
- 实测：`lint 汇总：通过 7/7，跳过 3，失败 0`（3 个内部输入无 Schema）。

## 4. 与规格的对齐记录

| 规格变化 | 实现动作 |
| --- | --- |
| 02 §4.2（Lead 指出 3 处不一致） | 状态机补 `scored -> invalid`（事件 `invalidate_post_hoc`）、`failed_infra -> pending/invalidate`（`requeue` 最多 3 次）、`paused_manual -> running/observing/verifying/invalid` 四条路径（通用 `resume` 按暂停前状态）；`failed_infra` 移出终态；CellStatus 增加 `manual` |
| 02 §6 P0-P5 | `state.evaluate_terminal(...)` 按优先级短路，P0/P5 作废、P1-P4 出分；测试覆盖全部分支 |
| 02 §7.1 预算字段冻结 | `Budget` 字段名与默认值逐字对齐（max_rounds/max_feedback_rounds/max_nudges/max_total_tokens/max_wall_clock_s/max_cost_usd/max_observe_s + escalation.*） |
| 03 §7.5 升级阈值 | 默认改为 `f1_no_progress_rounds=2、f2_no_progress_rounds=2`；partial 进展清零连续计数 |
| 03 §7.4 只反馈阻断缺陷 | `FeedbackLadder.select_target` 只看 functional/fatal；minor-only 返回 `no_blocking_defects` |
| 03 §9.1 need_feedback 口径 | 改为「feedback_rounds>=1 且已修复」的缺陷数；multi_round_f1 用 max_level_during_open=F1 且 feedback_rounds>=2 |
| 03 §9.2 P0-P14 | `grading.py` 按表逐条命中；G-INVALID-00/G-ANOMALY-98 不产出 grade |
| `grading-vectors.json` | V01-V12 全部转 unittest；V13（格式违规）作为补充用例；S01-S04 补齐 Failed/Abort/Anomaly/Invalid |
| grade.schema.json 字段改名 | `version->grade_version`、`cost{agent,judge,user,total,currency}`、`passed_hard_gates->hard_gate_summary`、`reasons->decision{rule_id,priority,rationale}`、新增 `tier_numeric/status/validity/counted_in_leaderboard/flags/defect_summary/feedback_summary/penalty_breakdown/consistency/judge_panel/supersedes/manual_notes` |
| defect.schema.json | `id->defect_id`、新增 `last_round/rounds_failed/fixed_round/feedback_rounds/max_level_during_open/progress/evidence[]` |
| feedback.schema.json | allowed_fields 改名 `f1_symptom/f2_evidence/f3_visual/f3_assist`；新增 kind(feedback|nudge)、feedback_index、text_hash、leak_check{rule_ids_hit,categories_hit,model_check,rewrite_count,fallback_used,final_action}、budget 快照 |
| 03 §9.9 / §10.4 | Cell 费用取 median(cost.agent)、<2 有效 epoch 转 Manual、作弊/格式违规带 * / † 标记、Pass 显示 `A(继承自 ...)` |
| 字段口径统一（isolation/Lead 同步） | 作弊权威口径 = `grade.flags` 含 `"cheat"`；`Grade.to_dict()` 不输出独立 `cheat_flag`；`trial.result.cheat_flag` 仅作为派生便利字段（本实现未写入，需要时从 flags 推导）；`report.py` 从 flags 读取 cheat/format_violation 并渲染 `*` / `†` 标记 |
| trial.schema.json 落地（isolation） | `examples/task-minimal/trial.json` 已通过 trial schema 校验：`rounds[].feedback` 整文件 `$ref` feedback schema、`result.grades[]` 整文件 `$ref` grade schema、`isolation` 段按 sandbox/canary/checks/leaks/network/secrets 构造；invalid 时 grades 为空数组的约束由适配器保证 |

## 5. 实跑证据

测试（2026-11-06 本机，Python 3.15.2）：

```text
$ cd llm-eval && python3 -m unittest discover -s reference/tests -t reference
...................................................................................................................................
----------------------------------------------------------------------
Ran 131 tests in 0.012s

OK
```

CLI（完整输出见 `../reference/README.md` §2）：`lint` 通过 7/7、`grade` 得到
`5/B+(12.34)`（G-F1-BPLUS）、`scan` 对含违规样例返回退出码 1 并列出
`cross_trial`/`egress`、`report` 生成 5 cell 的 markdown、
`simulate` 输出 3 轮 B+ 判分与金丝雀通过。

## 6. 已知限制与 TODO

| # | 限制 | 影响 | 后续 |
| --- | --- | --- | --- |
| L1 | 只解析 JSON，未内置 YAML 解析 | 题包 YAML 需先转 JSON | 适配层接 `yaml` 时只改 `adapters/`，判定代码不动 |
| L2 | trial.record 只序列化判分关心的字段 | 与 trial schema 完整字段有差距 | 结果落库由 Result Store 适配器补全；样例 trial 已过 schema |
| L3 | schema_lite 为子集 | 复杂 Schema 关键字未覆盖 | 生产可替换为 jsonschema，接口不变 |
| L4 | 未实现相似度检测（winnowing） | 隔离体检缺一项 | plan-v1 §5.3 第三步 |
| L5 | 价格表与真实 Harbor SDK 未接线 | cost 只透传 | `TODO(run-on-host)`：在 Harbor 环境用真实事件/计费验证映射 |
| L6 | 裁判面板未接真实多厂商 API | judge 用 MockJudge 离线验证 | 适配器实现 `JudgeClient` 协议即可 |
| L7 | 未实现多轮澄清问答计数上限（≤3 次后按 nudge） | 03 §7.8 的细节 | 后续在 `F0Policy` 增加计数器与测试 |

## 7. 与其它文档的接口

- 判定规则细节：`03-grading.md`（本文只记录落地差异）
- trial 生命周期：`02-eval-flow.md`
- 隔离与金丝雀：`04-isolation.md`（isolation 维护；trial schema 已落地）
- 题库与答案契约：`05-task-library.md`、`06-answer-and-acceptance.md`
- Harbor 映射：`07-harness-mapping.md` 与 `../reference/evalkit/adapters/harbor.py`