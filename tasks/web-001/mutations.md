# web-001 变异测试清单

要求 ≥5 个变异体被 Playwright 隐藏测试杀死（[docs/06-answer-and-acceptance.md](../../../docs/06-answer-and-acceptance.md) 第 5 节）。变异在参考解 A 上人工施加，执行 npx playwright test --config=hidden-tests/playwright.config.js（TODO(run-on-host)）。

| 编号 | 变异操作 | 期望被杀 | 杀死它的用例 |
| --- | --- | --- | --- |
| M1 | pointerup 不更新 state（只清理高亮） | 是 | T2/T3 拖拽无变化 |
| M2 | 落点判定改成追加到列尾（忽略目标卡片） | 是 | T2 顺序断言 |
| M3 | 去掉 localStorage 保存 | 是 | T2/T3/T5/T7 reload 断言 |
| M4 | 主题切换只改按钮文字不改 data-theme | 是 | T7 |
| M5 | 空状态永远 hidden | 是 | T6 |
| M6 | 计数不随渲染更新 | 是 | T1/T8 |
| M7 | 方向键只处理左右、不处理上下 | 是 | T5 |
| M8 | 给卡片加 pointer-events:none 阻止拖拽 | 是 | T2/T3 |
| M9 | 把 data-card-id 改成 data-id | 是 | T10/T2 |
| M10 | 在页面里 console.error 一句 | 是 | T1 |
| M11 | 拖拽后不渲染（state 变了 DOM 没变） | 是 | T2/T3 |
| M12 | 键盘移动后不 save | 是 | T5 reload |

## 变异分数

- 设计上 12 个变异体全部可被用例杀死；实际分数待镜像内执行（当前开发机无 Playwright，TODO(run-on-host)）。
- 无法被用例覆盖的变异：暗色对比度（R10 裁判）、拖拽高亮（R12 裁判），依赖截图与人工复核。