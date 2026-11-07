# 空 Agent 对照：web-001

## 1. 操作

空 Agent 不执行任何动作：不打开页面、不改文件、不回复。

## 2. 预期结果

| 项 | 预期 | 依据 |
| --- | --- | --- |
| 提交产物 | 工作区保持起始代码 | 无操作 |
| HG1 Playwright | false | T1-T10 中拖拽/持久化/主题/键盘等断言失败 |
| HG2 静态可运行 | true（起始页能打开） | 只是没实现交互 |
| HG3 DOM 契约 | true（未被破坏） | 没动文件 |
| 硬门槛总判定 | **Failed（F）** | required 硬门槛失败 |
| 软 rubric | 不执行 | 硬门槛短路 |

## 3. 本地复现

```bash
cd llm-eval/appendix/examples/web-001
APP_DIR="$PWD/workspace" bash hidden-tests/run.sh
# 当前开发机无 @playwright/test：静态契约检查会通过并 SKIP；
# 在预装 Playwright 的镜像内对起始代码跑完整用例，期望 HG1 fail。
```

## 4. 记录格式

`result.hard_gates=[{id: HG1, pass: false}, {id: HG2, pass: true}, {id: HG3, pass: true}]`、`result.grades[].tier='F'`、`validity='valid'`。