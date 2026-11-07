# 题包索引（tasks/）

> 状态：v3 · 维护：Lead · 最后更新：2026-11-07
> 上游：[00-conventions](../docs/00-conventions.md) §3 · 相关：[05-task-library](../docs/05-task-library.md)、[06-answer-and-acceptance](../docs/06-answer-and-acceptance.md)

本目录是 10 个**完整**题包（必需文件齐全，task/rubric/defects 均通过 Schema 终验，`task.yaml` 均声明 PRD FR-TSK-01 的 `runtime` 块）。每个题包目录内：

- `task.yaml` 元数据（含 `runtime.requirements` / `environment` / `execution_modes` / `model_requirements`）/ `instruction.md` 指令 / `persona.md` 模拟用户 / `rubric.yaml` 原子 rubric / `defects.yaml` 现象目录
- `answers/` 参考解 A、B（思路不同）+ 空 Agent 预期失败 `empty-agent.md`
- `hidden-tests/run.sh` 隐藏验收入口（默认对候选工程跑；`--selftest` 用于验证题包自身）
- `canary.md` 金丝雀埋点 / `notes.md` 构建、验收与局限说明；部分题另有 `workspace/`、`starter/`、`mutations.md`、`evidence/`

## 一览

| ID | 赛道 / 层 | 平台 | 硬门槛 | 本地验证 | 自检命令（仓库根目录） |
| --- | --- | --- | --- | --- | --- |
| [logic-001](logic-001/) | logic / logic | Linux 容器 | 参数化生成 + 求解器唯一解 + 答案比对 | ✅ 纯 Python | `bash tasks/logic-001/hidden-tests/run.sh` |
| [logic-002](logic-002/) | logic / logic | Linux 容器 | 折叠序列唯一解 + 答案比对 | ✅ 纯 Python | `bash tasks/logic-002/hidden-tests/run.sh` |
| [web-001](web-001/) | web / engineering | Linux 容器 | Playwright 拖拽/持久化/键盘/对比度 | 需浏览器（静态契约可本地跑） | `bash tasks/web-001/hidden-tests/run.sh` |
| [data-001](data-001/) | data / engineering | Linux 容器 | 隐藏数据集容差比对 | ✅ 纯 Python | `bash tasks/data-001/hidden-tests/run.sh` |
| [backend-001](backend-001/) | backend / engineering | Linux 容器 | API 测试 + 故障注入 | ✅ 纯 Python（模拟服务） | `bash tasks/backend-001/hidden-tests/run.sh --selftest` |
| [debug-001](debug-001/) | debug / engineering | Linux 容器 | 隐藏回归 + 测试哈希锁 + 变异 | ✅ 纯 Python | `bash tasks/debug-001/hidden-tests/run.sh --selftest` |
| [macos-001](macos-001/) | macos / platform | Tart macOS + 裸机性能机 | 裸机帧时间中位数 | 需 macOS/Xcode；本机实测基线 17.5fps | `bash tasks/macos-001/hidden-tests/run.sh --selftest` |
| [harmony-001](harmony-001/) | harmony / platform | Linux 构建 + 鸿蒙设备池 | HAP 构建签名 + 设备功能 | 需鸿蒙工具链/设备；C++ 数值测试可 host 跑 | `bash tasks/harmony-001/hidden-tests/run.sh --selftest` |
| [vulkan-001](vulkan-001/) | vulkan / platform | GPU 节点 | 验证层零报错 + 截图相似度 | 需 Vulkan SDK/GPU；CPU 数学与相似度工具可本地跑 | `bash tasks/vulkan-001/hidden-tests/run.sh --selftest` |
| [video-001](video-001/) | video / engineering | Linux 容器 | ffprobe 时长/分辨率/编码 + 抽帧 | 需 ffmpeg；无 ffmpeg 用样例自检 | `bash tasks/video-001/hidden-tests/run.sh --selftest` |

> 采用「`--selftest`」的题包会验证：参考解 A/B 通过、starter 失败、测试文件哈希锁生效、变异测试杀死、判分幂等。2026-11-07 的仓库检查在临时副本上运行了全部 10 个题包的自检命令，exit=0（web-001 按既有行为跳过 Playwright，仅做静态契约检查）。

## 使用方式

1. 先读该题 `instruction.md` 与 `notes.md`（含构建/运行/验收命令、已知局限、`TODO(run-on-host)` 位置）。
2. 本地自检（在仓库根目录执行）：`bash tasks/<task-id>/hidden-tests/run.sh --selftest`（若支持）；没有 selftest 的题默认跑候选工程。自检会写临时文件，建议先把题包复制到临时目录再跑。
3. 真实评测：按 [docs/07-harness-mapping.md](../docs/07-harness-mapping.md) 把题包映射为 Harbor task；隐藏测试与参考解只放验证侧，不得进入 Agent 镜像或 workspace。
4. 新增题包：按 [docs/05-task-library.md](../docs/05-task-library.md) 的出题流水线，最后跑 `python3 scripts/check_repo.py` 与 `uv run --frozen --no-sync python scripts/validate_schemas.py`。
