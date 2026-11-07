# macos-001 空 Agent 预期结果

| 门槛/条目 | 结果 | 依据 |
| --- | --- | --- |
| HG1 功能检查 | PASS | starter 粒子可见（非背景像素 400727 / 1920x1080 = 19.3%） |
| HG2 测试文件哈希 | PASS | 未修改 |
| HG3 性能门槛 | **FAIL** | 实测 Apple M6：avg_frame_ms=57.0（17.5fps），远低于 60fps 要求 |
| R1 合批 | FAIL | 仍逐粒子 draw call + 逐粒子 buffer |
| R2 降同步 | FAIL | 每帧 waitUntilCompleted |
| R3 帧时间 | FAIL | 57ms >> 16.7ms |
| R4 功能回归 | PASS | 画面元素未变 |

判定：硬门槛 HG3 失败 → **Failed**（性能题主门槛）。D1/D2 各 functional=3，合计 6 分。
