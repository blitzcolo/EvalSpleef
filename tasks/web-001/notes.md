# web-001 答案与验收说明

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 相关：[docs/06-answer-and-acceptance.md](../../docs/06-answer-and-acceptance.md)

## 1. 题目形态

- 起始代码：无构建单页看板（HTML/CSS/原生 JS），交互半成品；
- 目标：跨列拖拽 + 列内排序 + 刷新持久化 + 键盘四向移动 + 计数 + 空状态 + 暗色模式 + 不破坏 DOM 契约；
- 验收：Playwright 无头 Chromium 真实拖拽/按键/刷新（10 条用例），静态服务 python3 -m http.server。

## 2. 文件与职责

| 文件 | 侧 | 职责 |
| --- | --- | --- |
| `task.yaml` | 题包 | 元数据、隔离声明、预算、硬门槛 |
| `instruction.md` | Agent | 用户口吻需求 + 硬性约定 |
| `persona.md` | 用户 Agent | F0-F3 话术、FAQ、禁止泄露清单 |
| `rubric.yaml` | 判定 | 12 条原子 rubric |
| `defects.yaml` | 判定 | 覆盖 R1-R10、R12 的现象素材 |
| `workspace/` | Agent | 起始代码（index.html / app.js / styles.css / README） |
| `answers/reference-a.js` | 验证 | 原生 JS 完整实现（替换 app.js） |
| `answers/reference-b.jsx` | 验证 | React 函数组件实现（需构建，TODO(run-on-host)） |
| `answers/empty-agent.md` | 验证 | 空 Agent 预期 Failed |
| `hidden-tests/kanban.spec.js` | 验证 | 10 条 Playwright 用例（完整代码） |
| `hidden-tests/playwright.config.js` | 验证 | webServer + Chromium 配置 |
| `hidden-tests/run.sh` | 验证 | 入口：有 Playwright 跑用例，否则静态检查 + SKIP |
| `evidence/README.md` | 验证 | 截图与人工复核说明 |

## 3. 运行与复现

```bash
cd tasks/web-001

# 参考解 A：替换 app.js 后本地验证
cp answers/reference-a.js /tmp/reference-a.js
# 在装有 @playwright/test 的镜像内：
npm i -D @playwright/test && npx playwright install chromium
APP_DIR="$PWD/workspace" npx playwright test --config=hidden-tests/playwright.config.js

# 参考解 B（React）：构建后把 dist 作为 APP_DIR
# npm create vite@latest ... → 覆盖 src/App.jsx → npm run build
APP_DIR="$PWD/dist" npx playwright test --config=hidden-tests/playwright.config.js
```

当前开发机状态（2026-11-06）：Node v26.13.0 可用，但未安装 @playwright/test 与 Chromium，无法本地跑浏览器用例；`bash hidden-tests/run.sh` 走静态契约检查并输出 SKIP（exit 0）。完整交互验证 TODO(run-on-host)（全包 2 处：run.sh 与 reference-b 构建，满足 ≤3）。

已做的本地静态校验：
- `node --check` 通过：starter app.js、reference-a.js、kanban.spec.js、playwright.config.js；
- run.sh 静态检查：10 条用例标识齐全、DOM 契约 token 齐全、配置文件齐全。

## 4. 两份参考解为什么都该过

- A（原生 JS）：pointerdown/pointerup + elementFromPoint 落点判定 + localStorage；四向键、空状态、主题全部实现；DOM 契约与起始代码一致。
- B（React）：同一 DOM 契约与同一落点语义，用 hooks 状态管理；构建产物为静态页，Playwright 以 APP_DIR=dist 打开同一用例，行为一致。
- 两者不共享代码；差异集中在状态管理与渲染方式，验证「验收不认死一种写法」。

## 5. 容差与判定

| 项 | 口径 |
| --- | --- |
| 拖拽落点 | 拖到卡片上=插到该卡片前；拖到列区域=列尾（题面明说，无容差） |
| 持久化 | reload 后状态必须完全一致（无容差） |
| 暗色模式 | data-theme=dark 且 body 背景色变化（R7）；视觉对比度交裁判（R10，k=5 取中位） |
| 截图 | 6 张必采（evidence/README.md），只作为 R10/R12 证据 |

## 6. 已知限制

- 起始代码刻意保留半成品拖拽（pointerdown 高亮），Agent 若只补 pointerup 会缺落点计算与持久化，用例可区分。
- 拖拽语义（插到目标前）与常见库（sortable 插到前/后）略有差异，题面已明说；不做后插语义兼容。
- React 参考解需要构建链路，镜像内需预装 Node 依赖；当前只保证「同用例可通过」的设计，构建命令 TODO(run-on-host)。

## 7. 版本

- 1.0.0（2026-11-06）：首版。DOM 契约或拖拽语义变化 = MAJOR 重跑；新增用例 = MINOR 重判；样式文案 = PATCH。