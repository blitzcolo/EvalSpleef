# 附录 · 泄露检查规则集（LR-01..LR-14）

> 状态：v1 · 维护：grading · 最后更新：2026-11-06
> 上游：[03-grading](../../03-grading.md) §8 · 相关：[ladder-scripts](ladder-scripts.md) · [faq-policy](faq-policy.md) · [persona-template](persona-template.md)
> 可执行基准：[leak_rules_test.py](leak_rules_test.py)（33 个用例，标准库，退出码 0 = 全部通过）

本文件是 [docs/03-grading.md](../../03-grading.md) §8 的规则明细：正则、命中级别、正反例、误报处理与重写策略。**规则与测试脚本必须同步修改**；测试脚本是唯一可执行基准，文档解释为什么。

---

## 1. 检查管线（与 03 §8.2 对应）

```text
反馈文本（模板优先）
  → normalize（NFKC + 去零宽字符）
  → allowlist 掩码（persona.allowlist_phrases）
  → 按级别规则集扫描（F2/F3 先掩码预写日志透传区间）
  → 命中 block：重写，最多 3 次
  → 命中 refuse：不重写，发固定拒绝模板并记录
  → 小模型复核（clean/suspect/leak），suspect 按 leak 处理
  → 命中的规则 ID / 类别 / 匹配片段写入 feedback.leak_check
  → 仍失败：退回预写模板；模板也命中 ⇒ final_action=blocked
```

处理顺序不能反：**先 allowlist 再扫描**，否则用户可见文案（界面上的 Error 404）会被误杀；**F2/F3 必须先掩码透传区间**，否则日志原文里的文件名/函数名会误报。

---

## 2. 规则清单

字段说明：类别对应 [feedback.schema.json](../../../schemas/feedback.schema.json) 的 leak_category；动作 block=命中即重写，refuse=固定拒绝（不重写），warn=记录不阻断。

### LR-01 文件名/路径

- 类别：file_name ｜ 动作：block ｜ F0/F1 禁；F2/F3 仅限预写日志透传区间
- 正则（Python re，IGNORECASE + NFKC 归一化）：

```text
(?:^|[\s\"'(（])(?:[A-Za-z]:)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}\b
|(?<![\w./-])[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|json|ya?ml|md|rs|swift|m|mm|ets|cpp|cc|h|hpp|go|java|kt|sh|bash|html|css|toml|lock|mp4|mov|png|jpe?g|svg)\b
```

- 命中例：src/app/Board.tsx、package.json、hidden-tests/run.sh（同时命中 LR-07）
- 放行例：iPhone 上按钮有点小（产品名，allowlist 机制）、F1 赛车真好看（不匹配）
- 理由：文件名会把「去哪找」直接告诉模型，等于替它完成定位，超出 F1 允许的信息量。

> 编译注记（2026-11-06 终验修正）：LR-02 **按大小写敏感编译**。若套用 IGNORECASE，camelCase 分支会退化为「任意小写词」，把 token / CSV / Tab / CPU / GPU / JSON 等普通词误判为标识符；回归用例见 `leak_rules_test.py`。

### LR-02 函数名/标识符/代码符号

- 类别：function_name ｜ 动作：block ｜ F0/F1 禁；F2/F3 仅限透传区间
- 正则：

```text
(?<![Ff]\d)\b(?:[a-z][a-z0-9_]{2,}|[A-Z][A-Za-z0-9]{2,})\s*\([^)]*\)
|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b
|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b
```

- 命中例：board_state、rotateModel、flush()、useEffect（同时命中 LR-03）
- 放行例：iPhone（allowlist 产品名）、页面（中文不匹配）、F1（后接空格不构成调用）
- 注意：(?<![Ff]\d) 防止把「F1（初级）」这类反馈级别词误判为函数调用。

### LR-03 API/框架/工具名

- 类别：api_name ｜ 动作：block ｜ F0/F1 禁；F2/F3 仅限透传区间
- 正则：

```text
\b(?:useEffect|useState|useRef|addEventListener|requestAnimationFrame|fetch|axios|react|vue|svelte
|std::|vkCreate\w*|VkResult|MTLCommandBuffer|UIView|SwiftUI|ArkTS|NAPI|hvigorw|ohpm|hdc
|xcodebuild|cargo|gradle|webpack|vite|ffmpeg|ffprobe)\b
```

- 命中例：应该用 useEffect 重新渲染、cargo test 挂了、vkCreateBuffer 返回错误
- 放行例：F2 日志里的 cargo build 报错（透传区间内跳过本规则；LR-06 也跳过）
- 维护：词表随题库新增平台扩展（新增平台时必须补词，否则 F1 可绕过）。

### LR-04 日志/堆栈/错误原文

- 类别：log_text ｜ 动作：block ｜ F0/F1 禁；F2/F3 允许（属于该级别内容）
- 正则：

```text
Traceback \(most recent call last\)|File "[^"]+", line \d+|\bSegmentation fault\b|\bcore dumped\b
|\bpanic:|\bfatal error:|\b(?:Exception|ERROR|FATAL)\b|^\s*at\s+[\w.$]+\(.*\)
|exit (?:code|status) \d+|\b[A-Za-z_]+Error\b
```

- 命中例：TypeError: Cannot read properties of undefined、error[E0433]、exit code 1
- 放行例：F2/F3 中位于透传区间的原文；allowlist 只用于 UI 文案，不能用来放行堆栈
- 判定要点：**F1 只允许说「报错了」这种级别，不允许贴原文**；贴原文必须走 F2。

### LR-05 修复建议/根因

- 类别：fix_suggestion ｜ 动作：block ｜ **全级别禁止**
- 正则：

```text
(?:应该|应当|需要|建议|可以)(?:把|将|用|改成|改为|加上|删掉|修|补|改|调用|使用|设置)
|改成|改为|换成|替换成|加上|删掉|补上|加(?:个|上|一个)|改一下|修复一下|问题出在|根因
|你(?:可以)?试试|试试(?:在|把|用)|you should|try to|fix (?:it|this)
|change .{1,25} to|replace .{1,25} with|root cause
```

- 命中例：你试试在保存前先调用 flush()、这明显是持久化没做，加个 localStorage、改一下保存逻辑
- 放行例：我把卡片拖到最上面，刷新后又回弹了（只描述现象，不含建议）；我帮你点了一下签名按钮（F3 协助操作，不属于修复建议）
- 理由：给修复建议等于把解法告诉模型，会直接把档位拉到「不该给分」；这是最严重的泄露类型之一，不属于任何级别。

### LR-06 代码/补丁/命令

- 类别：code ｜ 动作：block ｜ F0/F1 禁；F2/F3 仅限透传区间
- 正则：

```text
~~~|\bdef\s+\w+\s*\(|\bclass\s+\w+
|(?:^|\n)\s*(?:import|from\s+\S+\s+import|#include|using|const|let|var|public|private|package)\s
|=>|===|!==|diff --git|npm (?:run|install)|cargo (?:build|test)|xcodebuild|hvigorw|python3?\s+\S+\.py
```

- 命中例：diff --git a/app.ts b/app.ts、npm run dev 启动不了、const next = ...
- 放行例：F2 日志里的 npm run build 报错（透传区间）；F1 用户说「按开始按钮」不算命令
- 维护：平台专属命令（hdc、hvigorw、swift build）加入词表。

### LR-07 参考答案/隐藏测试

- 类别：answer ｜ 动作：block ｜ **全级别禁止 + 事故升级**
- 正则：

```text
(?:正确答案|标准答案|参考答案|参考解|范文|reference answer|golden answer
|hidden[-_ ]?tests?|隐藏测试|隐藏数据|test_[a-z0-9_]+\.py|answers/|hidden-tests/)
```

- 命中例：hidden-tests/run.sh 失败了、参考解就是这么写的、test_public.py 第 3 行
- 处置：不仅重写，还要记 leak incident；人工复核是否已造成帮助泄露（§6）。

---

### LR-08 内部术语/评分规则

- 类别：internal_term ｜ 动作：block ｜ 全级别禁止
- 正则：

```text
(?:rubric|severity|judge|verifier|reward\.json|evaluator|缺陷|扣分|档位|反馈级别|评分规则|判分规则|权重)\b
|\bF[0-3]\s*(?:反馈|级别|级)
```

- 命中例：这条 rubric 判 fail、给你扣 3 分、反馈级别 F2
- 放行例：F1 赛车真好看、F1（一级方程式）——孤立 F1 不拦，只有带上下文才拦
- 理由：让被测模型知道评分规则会诱发 reward hacking（[Harbor reward hacking](https://docs.harborframework.com/news/reward-hacking.md)）。

### LR-09 金丝雀/其他 trial 标识

- 类别：canary ｜ 动作：block ｜ 全级别禁止 + 事故升级
- 正则：

```text
CNRY-[0-9a-fA-F]{8}-[0-9a-fA-F]{8}
|\b(?:logic|web|data|backend|debug|macos|ios|harmony|rust|vulkan|video|game)-\d{3}\.
|/tmp/(?:trial|eval)[\w./-]*
```

- 命中例：CNRY-9f3a1c02-7d4e0b11、web-001.claude-sonnet-5...
- 处置：金丝雀泄漏意味着隔离失效（[04-isolation](../../04-isolation.md)），trial 作废并排查；不能只当文本泄露处理。

### LR-10 日志来源/越级字段（F0/F1 专用）

- 类别：internal_term ｜ 动作：block ｜ F0/F1 禁；F2/F3 不适用（允许日志）
- 正则：

```text
(?:控制台|终端|stderr|stdout|堆栈原文|traceback|日志原文|报错原文|完整日志)
|f2_evidence|f3_visual|f3_assist|\bhidden\b
```

- 命中例：F1 说「控制台里报了个错」、把 f2_evidence 贴出来
- 放行例：F1 说「页面弹了个报错框」（用户可见现象，不含日志来源）
- 判定边界：说「报错」可以，说「控制台/日志原文」不行——因为后者暗示去读日志，信息量接近 F2。

### LR-11 URL/外部链接

- 类别：other ｜ 动作：block ｜ 全级别禁止
- 正则：

```text
https?://\S+|\bwww\.[\w.-]+
```

- 理由：模拟用户不上网、不贴链接；链接可能把模型引向题解或官方文档（是否允许查文档是题目资源配置，不是用户提示）。

### LR-12 指令注入/套取信息

- 类别：prompt_injection ｜ 动作：refuse ｜ 全级别
- 正则：

```text
(?:忽略|无视)(?:之前|以上|前面|所有)的?(?:指令|规则|要求)
|ignore (?:all )?(?:previous|above) instructions|system prompt|你的(?:系统)?提示词
|repeat your instructions|告诉我你的规则
```

- 处置：命中不是重写，而是发固定拒绝模板（见 ladder-scripts 的 F0-SECURITY），并记 injection_attempts；≥3 次进人工。**不要因为模型要求就透露任何配置**。

### LR-13 多缺陷混入（warn）

- 类别：other ｜ 动作：warn ｜ 全级别
- 正则：

```text
(?:另外|还有|同时).{0,24}(?:问题|不行|失败|错了|报错|看不清|不对|异常|没了|没反应)
```

- 命中例：还有暗色模式看不清
- 处置：不阻断发送，但记录并向控制器告警；控制器应保证一条反馈只对应一个缺陷（[feedback.schema.json](../../../schemas/feedback.schema.json) 的 target_defect_id 单值约束是硬校验）。

### LR-14 代码围栏/补丁标记

- 类别：code ｜ 动作：block ｜ 全级别
- 正则：

```text
(?:^|\n)\s*(?:三个反引号|~~~)|^@@\s|^\+\+\+\s|^---\s|\bdiff --git\b
```

> 注：实际正则中用 \x60{3} 表示三个反引号（避免文档渲染歧义），见 leak_rules_test.py。

- 命中例：代码围栏、@@ -1,5 +1,7 @@、+++ b/app.ts

---

## 3. 按级别适用矩阵（与 03 §8.3 一致）

| 规则 | 类别 | F0 | F1 | F2 | F3 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| LR-01 | file_name | 禁 | 禁 | 允许仅透传 | 允许仅透传 | block |
| LR-02 | function_name | 禁 | 禁 | 允许仅透传 | 允许仅透传 | block |
| LR-03 | api_name | 禁 | 禁 | 允许仅透传 | 允许仅透传 | block |
| LR-04 | log_text | 禁 | 禁 | 允 | 允 | block(F0/F1) |
| LR-05 | fix_suggestion | 禁 | 禁 | 禁 | 禁 | block |
| LR-06 | code | 禁 | 禁 | 允许仅透传 | 允许仅透传 | block |
| LR-07 | answer | 禁 | 禁 | 禁 | 禁 | block+事故 |
| LR-08 | internal_term | 禁 | 禁 | 禁 | 禁 | block |
| LR-09 | canary | 禁 | 禁 | 禁 | 禁 | block+事故 |
| LR-10 | internal_term | 禁 | 禁 | 不适用 | 不适用 | block(F0/F1) |
| LR-11 | other | 禁 | 禁 | 禁 | 禁 | block |
| LR-12 | prompt_injection | 拒答 | 拒答 | 拒答 | 拒答 | refuse |
| LR-13 | other | warn | warn | warn | warn | warn |
| LR-14 | code | 禁 | 禁 | 禁 | 禁 | block |

## 4. F2/F3 日志透传区间的规范

1. 透传内容只能来自 defects.yaml 的 f2_evidence.text / f3_visual.description，**不允许控制器自由生成**。
2. 拼接时用模板固定格式（「我这边看到一段报错：{log}」），并记录透传区间在最终文本中的字符偏移（feedback.leak_check 可存 extensions.log_spans）。
3. 检查器对透传区间掩码后再扫描；区间外仍执行全部规则。
4. 透传长度上限：f2_evidence ≤4000 字符（schema），建议实际 ≤800；超长日志先由出题人裁剪。
5. 透传内容仍要过 LR-07/08/09/11/12/14（全级别禁止）；若日志原文包含金丝雀或隐藏测试路径 ⇒ 不允许作为 F2 素材（出题人修复 defects.yaml）。

## 5. allowlist（误报处理）

- 来源：task.yaml 的 persona.allowlist_phrases（字符串数组，≤20 条，每条 ≤60 字）。
- 用途：用户可见文案本身像代码/日志的情况，例如界面上的「Error 404」「Timeout: retry」。
- 实现：扫描前把 allowlist 短语在文本中的出现位置掩码（替换为全角空格），不解码、不作语义判断。
- 禁止：把「文件名/函数名/日志片段」加进 allowlist 来绕过检查——allowlist 只服务于 UI 文案；每次新增必须走评审并在规则变更记录里说明。
- 误报记录字段：rule_id、matched、文本快照、人工判定（误报/真泄露）、处理人、日期。

---

## 6. 小模型复核提示词（模板）

```text
你是反馈泄露检查器。下面是「模拟用户」准备发给被测 Agent 的一条反馈，级别为 {level}。
该级别允许披露的内容：{allowed_fields}。
禁止披露：文件名/函数名/API 名/日志原文（F1）、修复建议/根因、代码/补丁、参考答案或隐藏测试、评分规则、其他 trial 标识。
请只回答 JSON：{"verdict":"clean|suspect|leak","reasons":["..."]}
判定标准：只要文本能让读者更接近「怎么修」或「答案是什么」，就是 leak；只描述用户看到的现象是 clean。
待检查文本：
{text}
```

- 复核模型：与被测模型、裁判面板均不同家族；固定版本；温度 0；相同输入要求同结论。
- suspect 按 leak 处理（宁严勿松）。

## 7. 重写策略

1. 命中 block 规则 ⇒ 让生成器重写：提示词只允许「减少信息」，不得新增事实；重写次数 +1。
2. 最多 3 次；第 3 次仍命中 ⇒ 退回预写模板原文（fallback_used=true）。
3. 模板仍命中 ⇒ final_action=blocked；控制器不再发送，trial 的 awaiting_feedback 转 verifying，按 G-ABORT-04 判 Failed，开反馈事故。
4. 命中 refuse 规则（LR-12）⇒ 不重写，直接用固定拒绝模板（F0-SECURITY）。
5. 所有尝试（含被阻止文本）归档；text_hash 写入 feedback 记录。

重写提示词（只允许减少信息）：

```text
下面一条反馈没有通过泄露检查（命中：{rule_ids}）。请改写为同一现象的**更少信息**版本：
- 保留：用户看到的操作与现象、期望（F1 允许范围内）；
- 删除：任何文件名、函数名、API 名、日志原文、根因、修复建议、代码、测试信息；
- 不得新增事实，不得改变现象描述的含义。
只输出改写后的正文。
```

## 8. 测试证据

命令与结果（2026-11-06 实跑）：

```bash
cd llm-eval
python3 appendix/personas/leak_rules_test.py
# 33/33 cases passed; rules=14   exit=0
```

测试覆盖：14 条规则均至少 1 个正例；F1/F2/F3 正常现象话术、产品名误报、F1 赛车误报、F2 日志透传、F3 视觉/协助、金丝雀、注入尝试、UI 文案 allowlist 共 33 个用例。新增/修改规则时必须先改测试并跑通，再改本文件。

## 9. 变更流程

1. 提 issue：规则 ID、触发案例、影响级别、是否事故。
2. 改 leak_rules_test.py 加用例（正例+反例），跑通。
3. 改本文件与 [03-grading](../../03-grading.md) §8.3 矩阵。
4. 版本记录：规则集版本写入 feedback.generation.prompt_version 与批次冻结记录；规则变更后需用锚点集重判 30-50 条历史反馈做回归。
