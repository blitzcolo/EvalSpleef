# 附录 · 全仓机器可读契约终验报告（task-8）

> 状态：v1 · 维护：grading · 最后更新：2026-11-06
> 上游：[00-conventions](../00-conventions.md) §3/§4 · [03-grading](../03-grading.md) · 相关：[schemas/](../../schemas/) · [examples/](../../tasks/)
> Schema 校验工具：`scripts/validate_schemas.py`（PyYAML + jsonschema Draft 2020-12，venv `/tmp/llmeval-venv`）。**本轮 Schema 结论以该脚本输出为准，本报告不重复实现。**

---

## 1. 结论摘要

- 校验快照：2026-11-06；题包 10 个、文档 30 个（task.yaml / rubric.yaml / defects.yaml 各一）。
- 结果：**PASS**；Schema 失败文档 0 个、涉及题包 0 个。
- reference/examples：脚本只覆盖顶层 2 个 JSON（`canary-trials.json`、`grading-case.json` → `[SKIP]` 人审）；嵌套 `task-minimal/*.json` 由本报告补充校验（见 §7.1）。
- validator 原始输出末行：`结果：PASS（共校验 30 个文档）`（退出码 0）。
- 补充契约检查：必需文件 全部齐备；Agent 可见文件隐藏测试泄漏 未发现；阻断条目 > max_feedback_rounds 的题包 9 个（设计层 WARN，见 §6.1）。
- reference 单测：PASS（Ran 131 tests / OK）。
- grading-vectors：12 个基础用例 + 1 个补充用例；tier/tier_numeric 一致性错误 0 处。

## 2. 校验方法与工具链

| 项 | 内容 |
| --- | --- |
| Schema 校验器 | `scripts/validate_schemas.py`：`Draft202012Validator` + `referencing.Registry`；先对 6 个 Schema 做 `check_schema()` 自检，再按 `$id` 解析相对 `$ref` 校验题包文档 |
| YAML 解析 | PyYAML `safe_load`。注意：未加引号的 `2026-11-06` 会被解析成 `datetime.date`，而 Schema 要求 string —— 本轮曾因此报错，已全部修复（当前快照 0 处） |
| 补充检查 | 同一 venv 下复算：必需文件、引用文件存在性、task_id 一致性、缺陷目录覆盖、阻断条目/反馈预算 lint、f1_symptom 泄露扫描、Agent 可见文件隐藏测试扫描、reference 嵌套样例、grading-vectors 不变式 |
| 交叉验证 | 安装 jsonschema 前，grading 用自写极简校验器（24 关键字）与 impl 的 `evalkit/schema_lite.py` 对 21 个题包文档做过双校验：结论 **21/21 一致**；终验以官方 `jsonschema` 为准 |
| 校验边界 | Schema 只证明结构与枚举合法，不证明语义正确（rubric 是否单命题、严重度是否合理、f1_symptom 是否真的用户可见）；见 §9 |

## 3. 题包总表

| 题包 | task.yaml | rubric.yaml | defects.yaml | 必需文件 | Schema 结论 |
| --- | --- | --- | --- | --- | --- |
| backend-001 | PASS | PASS | PASS | 齐备 | PASS |
| data-001 | PASS | PASS | PASS | 齐备 | PASS |
| debug-001 | PASS | PASS | PASS | 齐备 | PASS |
| harmony-001 | PASS | PASS | PASS | 齐备 | PASS |
| logic-001 | PASS | PASS | PASS | 齐备 | PASS |
| logic-002 | PASS | PASS | PASS | 齐备 | PASS |
| macos-001 | PASS | PASS | PASS | 齐备 | PASS |
| video-001 | PASS | PASS | PASS | 齐备 | PASS |
| vulkan-001 | PASS | PASS | PASS | 齐备 | PASS |
| web-001 | PASS | PASS | PASS | 齐备 | PASS |

## 4. 失败项与修复建议（Schema）

**无失败项**：10 个题包 × task/rubric/defects 共 30 个文档全部通过 Schema 校验。

## 5. Schema 覆盖范围与解析注意事项

`jsonschema` 官方实现天然覆盖 draft 2020-12 全部关键字，因此本轮**没有关键字覆盖缺口**。本轮实际触发/依赖的关键字：

`$ref`、`type`、`required`、`properties`、`additionalProperties`、`items`、`minItems/maxItems`、`uniqueItems`、`enum`、`const`、`pattern`、`minLength/maxLength`、`minimum/maximum`、`allOf/anyOf/oneOf`、`if/then/else`、`not`、`format`（date、date-time）。

本轮 Schema 增补（Lead 决策）：`rubric.schema.json` 的 `rubric_item` 新增可选 `detect`（object：`source` + `check_ids[]`），用于 rubric 条目 ↔ 验证实现/检查 ID 的映射；`docs/03-grading.md` §5.2 字段表已同步。该字段不参与判分。

YAML 解析注意事项（本轮实际踩到）：

| 写法 | PyYAML 结果 | Schema 期望 | 修复 |
| --- | --- | --- | --- |
| `created: 2026-11-06` | `datetime.date` | string | 加引号 |
| `tags: [rate_limit]` | string | task 用 `^[a-z][a-z0-9-]{1,23}$`、rubric 用 `^[a-z][a-z0-9_]{1,23}$` | 各自改用连字符/下划线；两个 Schema 的 tags 模式不一致，见 §6.5 |

## 6. 跨文件契约与设计一致性（补充检查）

以下不属于 JSON Schema 能表达的范围，由本报告单独复算，**不改变** §1 的 Schema 结论。

### 6.1 阻断条目数 vs 反馈预算（设计层 WARN）

docs/03 §5.6 的 lint 建议：阻断条目（functional+fatal）数 ≤ `budget.max_feedback_rounds`。否则「每轮只讲一个缺陷」的控制器在预算内无法覆盖全部阻断条目，档位会退化为 Failed。

| 题包 | 阻断条目 | max_feedback_rounds | 满足 lint? |
| --- | --- | --- | --- |
| backend-001 | 10 | 3 | 否（WARN） |
| data-001 | 9 | 3 | 否（WARN） |
| debug-001 | 8 | 3 | 否（WARN） |
| harmony-001 | 6 | 4 | 否（WARN） |
| logic-001 | 8 | 3 | 否（WARN） |
| logic-002 | 7 | 3 | 否（WARN） |
| macos-001 | 6 | 3 | 否（WARN） |
| video-001 | 7 | 3 | 否（WARN） |
| vulkan-001 | 7 | 7 | 是 |
| web-001 | 9 | 4 | 否（WARN） |

> 这是**规格层问题**：需在「提高 max_feedback_rounds / 减少阻断条目 / 允许一条反馈覆盖多个缺陷（改变档位语义）」三者中拍板。建议至少满足 `max_feedback_rounds ≥ 阻断条目数`。

### 6.2 缺陷目录覆盖

| 题包 | rubric 条目 | 阻断条目 | defects.yaml 条目 | 阻断条目无现象素材 | task_id 一致 |
| --- | --- | --- | --- | --- | --- |
| backend-001 | 13 | 10 | 10 | — | 是 |
| data-001 | 12 | 9 | 10 | — | 是 |
| debug-001 | 11 | 8 | 8 | — | 是 |
| harmony-001 | 12 | 6 | 12 | — | 是 |
| logic-001 | 12 | 8 | 9 | — | 是 |
| logic-002 | 10 | 7 | 8 | — | 是 |
| macos-001 | 9 | 6 | 6 | — | 是 |
| video-001 | 10 | 7 | 7 | — | 是 |
| vulkan-001 | 11 | 7 | 11 | — | 是 |
| web-001 | 12 | 9 | 11 | — | 是 |

> 复验更新（2026-11-06 稍晚快照）：tasks-a/tasks-b 已补齐全部阻断条目的现象素材，**10 个题包覆盖率 100%，本表缺口列全部为 —**；`defects.yaml` 条目数与 rubric 条目的关系不再构成 feedback_blocked 风险。

### 6.3 f1_symptom 泄露规则扫描（WARN，需人工确认）

用 [leak-rules](personas/leak-rules.md) 规则集对全部 f1_symptom 做 F1 级别扫描：12 条命中。**这些是规则集误报（普通英文词被 LR-02 当成标识符），不是题包缺陷**：

| 题包 | 条目 | 命中词 | 规则 |
| --- | --- | --- | --- |
| backend-001 | R1 | token | LR-02 |
| backend-001 | R4 | Idempotency、Key | LR-02 |
| backend-001 | R5 | body、key | LR-02 |
| backend-001 | R6 | SKU | LR-02 |
| backend-001 | R7 | token | LR-02 |
| backend-001 | R9 | health | LR-02 |
| debug-001 | R7 | CSV | LR-02 |
| macos-001 | R3 | JSON | LR-02 |
| macos-001 | R5 | CPU | LR-02 |
| macos-001 | R6 | GPU | LR-02 |
| web-001 | R4 | Tab | LR-02 |
| web-001 | R9 | 控制台 | LR-10 |

> 根因：LR-02 的 camelCase 分支在 `re.IGNORECASE` 下会把 token / CSV / Tab / CPU / GPU / JSON 等普通英文词误判为标识符。修复建议（grading 后续任务）：LR-02 单独用大小写敏感编译 + 英文常用词白名单；不作为题包修复项。

### 6.4 Agent 可见文件中的隐藏测试泄漏

未发现：`instruction.md` 与 `workspace/starter` 中均未出现「隐藏测试/隐藏数据/参考答案」字样。

补充说明：所有题包的 `persona.md` 都按设计写了「不得提及隐藏测试」之类的**禁止泄露规则**（persona 属于用户侧文件，不是 Agent 可见文件），不属于泄漏。

### 6.5 必需文件、可执行位与参考解

| 题包 | 必需文件 | run.sh 可执行 | reference-a / reference-b |
| --- | --- | --- | --- |
| backend-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| data-001 | 齐备 | 是 | reference-a.py · reference-b.py |
| debug-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| harmony-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| logic-001 | 齐备 | 是 | reference-a.py · reference-b.py |
| logic-002 | 齐备 | 是 | reference-a.py · reference-b.py |
| macos-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| video-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| vulkan-001 | 齐备 | 是 | reference-a.patch · reference-b.patch |
| web-001 | 齐备 | 是 | reference-a.js · reference-b.jsx |

## 7. reference/ 与 evalkit 抽查

### 7.1 reference/examples/task-minimal（嵌套 JSON）

`validate_schemas.py` 只 glob `reference/examples/*.json`（顶层），`task-minimal/` 子目录不在其覆盖内；本报告用同一 `Draft202012Validator` 补充校验：

| 文件 | Schema | 结果 |
| --- | --- | --- |
| task-minimal/defect-catalog.json | defect.schema.json | PASS |
| task-minimal/defect-observation.json | defect.schema.json | PASS |
| task-minimal/feedback.json | feedback.schema.json | PASS |
| task-minimal/grade.json | grade.schema.json | PASS |
| task-minimal/rubric.json | rubric.schema.json | PASS |
| task-minimal/task.json | task.schema.json | PASS |
| task-minimal/trial.json | trial.schema.json | PASS |

### 7.2 evalkit 字段一致性抽查（只读 grep）

| Schema 字段 | 在 reference/evalkit/*.py 出现 |
| --- | --- |
| grade_version | 是 |
| tier_numeric | 是 |
| cheat_flag | 是 |
| penalty_breakdown | 是 |
| target_defect_id | 是 |
| feedback_index | 是 |
| f1_symptom | 是 |
| defect_id | 是 |
| made_progress | 是 |
| catalog_version | 否（建议确认） |

> 抽查文件：__init__.py, __main__.py, canary.py, cli.py, defects.py, feedback.py, grading.py, judge.py, models.py, report.py, schema_lite.py, state.py。`catalog_version` 未在代码中出现：defects.yaml 的目录版本字段目前未被运行时读取；若后续要做目录版本冻结校验需补。

### 7.3 reference 单测

- 命令：`python3 -m unittest discover -s reference/tests -t reference`
- 结果：PASS；tail：`Ran 131 tests / OK`

## 8. grading-vectors 抽查

- 基础用例 12 个 + 补充用例 1 个；tier/tier_numeric 一致性错误 0 处。
- 覆盖的 rule_id：G-BUDGET-F, G-CHEAT-01, G-F0-MINOR, G-F0-PERFECT, G-F1-B, G-F1-BPLUS, G-F1-C, G-F1-CPLUS, G-F2-D, G-F3-D, G-FORMAT-05, G-HUMAN-99。
- 说明：`G-INVALID-00` / `G-ANOMALY-98` 按 docs/03 §9.2 不产生 grade，因此不出现在 vectors 的 expected 中（无 grade 用例由状态机测试覆盖）。

## 9. 未覆盖与需人工确认清单

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | `reference/examples` 顶层 2 个 JSON | 无对应 Schema（脚本 `[SKIP]`）：`canary-trials.json` 是金丝雀输入、`grading-case.json` 是算 grade 的输入 fixture；建议后续为 fixture 增 input Schema 或纳入 reference/tests 断言 |
| 2 | 语义正确性 | Schema 不检查 rubric 是否单命题、严重度是否合理、f1_symptom 是否真的用户可观察、`detect.check_ids` 是否真实存在 |
| 3 | hidden-tests 实际可执行性 | 只检查存在与可执行位，未在本题环境运行（需真实工具链；见各题包 notes.md 的 TODO(run-on-host)） |
| 4 | 平台题真机验证 | macos/harmony/vulkan 的构建、签名、设备运行不在本次校验范围内 |
| 5 | f1_symptom 误报 | §6.3 的命中需人工确认（当前判断：全是规则集误报） |
| 6 | 阻断条目 vs 预算 | §6.1 属规格决策，Schema 无法表达 |

## 10. 复现命令

```bash
cd llm-eval
python3 -m venv /tmp/llmeval-venv
/tmp/llmeval-venv/bin/pip install pyyaml jsonschema
/tmp/llmeval-venv/bin/python scripts/validate_schemas.py      # Schema 终验（本报告 §3/§4 来源）
python3 -m unittest discover -s reference/tests -t reference   # 参考实现单测
python3 appendix/personas/leak_rules_test.py                   # 泄露规则集回归（33 用例）
```

## 11. 快照与变更记录

- 2026-11-06：首轮校验（自写极简校验器 + impl schema_lite 交叉验证）：9 个题包 16 处错误、19 条 WARN；据此分派 tasks-a/tasks-b/research 修复。
- 2026-11-06：`rubric.schema.json` 增补 `detect`（Lead 决策）、docs/03 §5.2 同步；改用官方 `jsonschema` 复验，题包机械问题陆续修复。
- 2026-11-06：本报告快照 —— Schema 失败 0 处 / 题包 0 个；vulkan-001 纳入 10 题包范围。修复来源：tasks-a（rubric detect/类型/合取/枚举 + defects 覆盖）、tasks-b（isolation/日期引号/tags/YAML 转义 + defects 覆盖）、research（vulkan resources.gpu）。
- 2026-11-06（补记）：WARN#1（defects.yaml 现象素材缺口）已全部闭环 —— 10/10 题包阻断条目零缺失；`scripts/validate_schemas.py` 复跑仍 PASS（30 文档）。剩余 WARN 仅 §6.1（阻断条目 vs 反馈预算，待 Lead 拍板）与 §6.3（规则集误报，待 grading 修规则）。
- 校验不修改任何被校验文件；写作用域仅本报告（另有 Lead 授权的 `rubric.schema.json` / `docs/03-grading.md` 增补）。

## 12. Lead 裁决与最终状态（2026-11-06）

### 12.1 三项待决策的裁决

| # | 议题 | 裁决 | 落地 |
| --- | --- | --- | --- |
| 1 | 阻断条目数 > max_feedback_rounds（9/10 题包） | **不提高预算、不合并 rubric、不允许多缺陷单轮**。澄清约束口径：只有 **L2 软性阻断条目（llm_judge/agent_judge 且 functional/fatal）** 受 max_feedback_rounds 约束；L1 脚本阻断条目按「构建/运行/测试/产物」聚合现象反馈，不逐条占反馈轮（上限建议 12）。实测 10 个题包软性阻断条目为 0–2 条，全部满足。 | docs/03 §5.6 与 §11 已改；无需改题包 |
| 2 | f1_symptom 规则集误报（LR-02） | 根因确认：COMPILED 对全部规则套 IGNORECASE，导致 camelCase 分支退化为「任意小写词」，token/CSV/Tab/CPU/GPU/JSON 被误判。修复：LR-02 改为大小写敏感编译，并补 2 条回归用例；leak-rules.md 加编译注记。 | appendix/personas/leak_rules_test.py（35/35 PASS）、leak-rules.md |
| 3 | tags 模式不一致 | 统一为 `^[a-z][a-z0-9_-]{1,23}$`（同时允许连字符与下划线，兼容既有题包）。 | schemas/task.schema.json、schemas/rubric.schema.json，30 文档复验 PASS |

### 12.2 最终验证证据（裁决后复跑）

```text
$ /tmp/llmeval-venv/bin/python scripts/validate_schemas.py
结果：PASS（共校验 30 个文档）

$ python3 scripts/check_repo.py --run
结果：0 error / 0 warning
[run] 参考实现单测：Ran 131 tests / OK
[run] backend/debug/data/logic-001/logic-002/harmony/macos/video/vulkan/web 10 个题包 exit=0

$ python3 appendix/personas/leak_rules_test.py
35/35 cases passed; rules=14
```

### 12.3 剩余已知限制（接受，不阻塞交付）

- `reference/examples` 的 2 个 fixture（canary-trials / grading-case）无对应 Schema，按人审处理（§9）。
- 平台相关验收需在真实工具链执行：macos 性能机实测、harmony 设备侧 `--device`、vulkan GPU 节点参考图生成；均在各题包 `notes.md` 标注 `TODO(run-on-host)`。
- evalkit 尚未读取 defect `catalog_version`（弱一致性），记入 docs/08 已知限制。
- 题库 30 题中仅 10 题带完整题包（其余为 draft 目录），上线前需按出题流水线补齐。
