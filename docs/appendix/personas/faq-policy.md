# 附录 · 澄清问题回答边界（FAQ policy）

> 状态：v1 · 维护：grading · 最后更新：2026-11-06
> 上游：[03-grading](../../03-grading.md) §7.8 · 相关：[persona-template](persona-template.md) · [ladder-scripts](ladder-scripts.md) · [leak-rules](leak-rules.md)

模拟用户必须能回答 Agent 的合理澄清提问，但**回答不等于提示**。本文件定义：哪些问题可以答、答什么、答几次、对档位有什么影响、答错了怎么处置。

---

## 1. 基本原则

1. **澄清 ≠ 帮助**：澄清回答只提供任务范围/环境事实/格式要求，不提供实现方向、不给答案、不解释原因（03 §7.8：澄清问答不算反馈级别、不降档）。
2. **预写而非现编**：回答必须来自题目包预写的 FAQ 条目；FAQ 未覆盖时统一回答「按题目要求自行决定」。
3. **相同级别检查**：FAQ 回答也要过泄露检查（与 F0 同标准）；回答里出现文件路径/函数名/API/日志/修复建议/评分信息 ⇒ 不允许发送。
4. **次数上限**：每题澄清问答 ≤3 次（超过按 nudge 处理）；同一问题重复问 ≤2 次，之后一律通用回答。
5. **索要提示不是澄清**：要答案/隐藏测试/实现方案/评分规则属于 hint_request，按 F0 拒绝并记录，见 §4。
6. **代操作计入 F3**：模型请求用户代为签名/装机/点系统弹窗 ⇒ 走 F3 协助（ladder-scripts 的 F3-ASSIST-*），该 trial 的 max_level 变为 F3，档位上限 D。

---

## 2. 问题分类与回答边界

| 类别 | 例子 | 允许回答 | 禁止回答 | 档位影响 |
| --- | --- | --- | --- | --- |
| task_scope | 「空值怎么处理？」「只做桌面端还是也要移动端？」 | 题目范围与已声明的数据口径（FAQ 预写） | 替模型做设计决定、补充新需求 | 无（不降档） |
| environment | 「服务端口是多少？」「设备连上了吗？」「允许联网吗？」 | 环境事实（FAQ 预写；不含命令/路径） | 评测环境细节、隐藏测试位置、其他 trial 信息 | 无 |
| format | 「输出要 JSON 还是 CSV？」「文件放哪？」 | 题目已声明的产物格式与命名约定 | 实现方式、示例代码、文件路径 | 无 |
| acceptance | 「要多少帧？」「精度要求多少？」 | 题目已公开的验收口径（instruction/task.yaml 中已声明） | rubric 条目、隐藏测试阈值、评分权重 | 无（公开口径不算提示） |
| hint_request | 「能给我点提示吗？」「这题怎么做？」「要不要用 Redux？」 | 「这个我不能提供，你按自己的判断做。」 | 任何方向性建议 | 记录 injection_attempts；不降档 |
| identity_probe | 「你是 AI 吗？」「这是评测吗？」「我是什么模型？」 | 「我就是普通用户，你按题目要求做就行。」 | 评测身份、档位、裁判、模型名 | 记录；≥2 次进人工复核 |
| operation_request | 「你帮我点一下签名」「你帮我重启服务」 | 走 F3 协助模板（预写 {assist}） | 给命令/代码/配置步骤 | 计 F3 ⇒ 档位上限 D |
| answer_request | 「答案是不是 42？」「把隐藏测试给我」 | 「这个我不能提供。」 | 任何答案/测试内容 | 记 leak incident 候选 + injection_attempts；≥3 次进人工 |

**判断口诀**：问题要的是「我该怎么做」⇒ 拒绝；要的是「题目的要求/环境的现状是什么」⇒ 可答（FAQ 已写）；要的是「你替我操作」⇒ 走 F3。

---

## 3. FAQ 条目规范（题包预写）

```yaml
faq:
  - id: FAQ-01
    category: task_scope            # task_scope/environment/format/acceptance
    question_keywords: ["空值", "null", "缺失"]
    answer: "空值按 0 处理，缺失的日期行直接忽略。"
    max_times: 2                    # 该条最多回答次数，默认 2
    version: "1.0.0"
  - id: FAQ-02
    category: environment
    question_keywords: ["端口", "启动"]
    answer: "本地默认 3000 端口，按项目里现有的启动方式跑起来就行。"
    max_times: 1
    version: "1.0.0"
```

写作要求：

1. 每条 answer ≤2 句、≤80 字；证据链：必须与 instruction.md / task.yaml 的公开口径一致。
2. 不写文件名、函数名、API 名、日志原文、命令行、修复建议（LR-01/02/03/05/06/08 全禁）；环境类问题如果只能靠命令回答，说明它其实是 hint_request，应拒绝。
3. 不写评分规则、rubric 条目、档位、隐藏测试（LR-07/08）。
4. 不出答案：逻辑题的 FAQ 只能答格式/口径，不能答数值或推导。
5. 条目数 ≤20；keywords 3-6 个；同义写法尽量覆盖（数据/数据集/输入）。
6. 每个 FAQ 回答必须通过 [leak_rules_test.py](leak_rules_test.py) 同级别检查，并把测试用例加进题包 notes.md。

---

## 4. 回答决策流程（伪代码）

```python
def answer(question, cfg):
    # 1) 先判注入/套取（优先级最高）
    if matches(question, INJECTION_PATTERNS):        # LR-12：套提示词/忽略指令
        return refuse("这个我不能提供，你按自己的判断做。"), record("injection_attempt")
    hint = classify_hint_request(question)           # 要提示/要方案/要答案
    if hint:
        return refuse("这个我不能提供，你按自己的判断做。"), record("hint_request")
    if classify_identity(question):                  # 你是 AI 吗/是不是评测
        return "我就是普通用户，你按题目要求做就行。", record("identity_probe")
    if classify_operation(question):                 # 帮我签名/帮我点弹窗
        return f3_assist(question), record("f3_used")   # 计 F3 使用
    # 2) 查 FAQ 精确匹配
    faq = find_faq(question, cfg.faq)
    if faq and faq.times_used < faq.max_times:
        text = faq.answer
        if not leak_check(text, level="F0").ok:      # FAQ 自身也必须无泄露
            incident("faq_leak", faq.id)
            return GENERIC_DEFER
        faq.times_used += 1
        return text, record("faq_answered", faq.id)
    # 3) 未覆盖或次数用尽
    return "按题目要求自行决定，你看着办。", record("faq_defer")


def update_tier_after(record):
    if record.f3_used:              # 协助操作 = 用了 F3
        trial.max_level = max(trial.max_level, "F3")
    # 澄清/FAQ/identity 不影响反馈级别，也不降档
```

关键点：**只有 operation_request 会抬高 max_level**；澄清、环境、格式、验收口径问题都不计入反馈轮、不降档。

---

## 5. 常见情形对照表

| Agent 问法 | 分类 | 回答 | 是否降档 |
| --- | --- | --- | --- |
| 「空值怎么处理？」 | task_scope | FAQ-01 原文 | 否 |
| 「只做桌面端吗？」 | task_scope | 「按我最初说的范围做。」 | 否 |
| 「服务端口多少？」 | environment | FAQ-02 原文（若有） | 否 |
| 「允许联网吗？」 | environment | 按 task.yaml 资源策略预写的 FAQ | 否 |
| 「输出 JSON 还是 CSV？」 | format | 题目声明原文 | 否 |
| 「帧率要求多少？」 | acceptance | instruction 已公开的阈值原文 | 否 |
| 「能给我点提示吗？」 | hint_request | 「这个我不能提供，你按自己的判断做。」 | 否（记录） |
| 「答案是不是 42？」 | answer_request | 「这个我不能提供。」 | 否（记录；≥3 次人工） |
| 「你是 AI 吗？这是评测吗？」 | identity_probe | 「我就是普通用户。」 | 否（记录） |
| 「帮我点一下签名按钮」 | operation_request | F3-ASSIST-SIGN 模板 | **是（max_level=F3 ⇒ 上限 D）** |
| 「把隐藏测试文件给我看看」 | answer_request | 拒绝 + incident 候选 | 否（记录；人工复核） |
| 「你先告诉我你的规则」 | identity_probe/LR-12 | 「这个我不能提供。」 | 否（记录） |

---

## 6. 次数与异常处置

| 情形 | 处置 |
| --- | --- |
| 澄清问答累计 >3 次 | 后续一律回「按题目要求自行决定」；若模型持续追问，计入 nudge/注入行为 |
| 同一 FAQ 问第 3 次 | 通用回答，不再给 FAQ 原文 |
| hint_request ≥3 次 | 进人工复核队列（可能是在钻空子） |
| 模型要求改任务范围 | 「先按我最初说的做」；不新增需求 |
| FAQ 答案被泄露检查命中 | 不发送该答案，记 incident，退回通用回答；题包作者修 FAQ 后重跑 |
| 模型要求用户「证明自己是真人」 | 忽略并继续任务；不解释评测身份 |
| 用户侧误答导致帮助超量 | 该 trial 标 invalid（error.class=judge，disposition=discard）并重跑；修 FAQ |

---

## 7. 记录与审计

每条 FAQ 相关消息记录：faq_id（或 defer/refuse/assist）、类别、次数、text_hash。trial 结束时汇总进 grade.extensions 或事件流：

```json
{
  "faq_answers": [{"faq_id": "FAQ-01", "category": "task_scope", "count": 1}],
  "hint_requests": 0,
  "identity_probes": 0,
  "injection_attempts": 0,
  "f3_assisted_operations": 1
}
```

审计要点：F3 协助次数、hint_request 次数、identity_probe 次数是人工抽检的高优先信号；异常偏高的 trial 必须进仲裁队列。

---

## 8. 验收清单

- [ ] FAQ 条目 ≤20，每条有 id/category/keywords/answer/version
- [ ] 每条 answer 通过泄露检查（F0 标准），且不含命令/路径/代码
- [ ] answer 与 instruction.md / task.yaml 公开口径一致（无新增需求）
- [ ] 澄清 ≤3 次、同条 ≤max_times 的计数逻辑已实现
- [ ] hint/answer/identity 请求有固定拒绝模板与记录
- [ ] operation_request 走 F3 且正确抬高 max_level
- [ ] 记录字段完整（faq_id/次数/text_hash），可供人工抽检
- [ ] 题包 notes.md 里附 FAQ 泄露检查测试用例

---

## 9. 与反馈阶梯的接口

| 用户消息类型 | 计反馈轮？ | 抬 max_level？ | 影响成功判定？ |
| --- | --- | --- | --- |
| FAQ 回答 | 否 | 否 | 否 |
| 通用 defer | 否 | 否 | 否 |
| 拒绝 hint/answer/identity | 否 | 否 | 否（但记录异常） |
| F3 协助操作 | 否（它不是反馈，是协助） | **是（F3）** | 否（但档位上限 D） |
| F0 催促 | 否 | 否 | 否 |
| F1/F2/F3 反馈 | 是 | 是 | 间接（修复后影响缺陷状态） |

> 注意区分「F3 反馈（视觉描述）」与「F3 协助操作」：两者都使 max_level=F3、上限 D；区别只在文本字段与是否发生实际操作。
