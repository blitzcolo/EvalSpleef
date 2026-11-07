# video-001 空 Agent 预期结果

| 门槛/条目 | 结果 | 依据 |
| --- | --- | --- |
| HG1 ffprobe 硬门槛 | **FAIL** | starter 导出 1280x720/25fps/mpeg4/24s，全部不达标 |
| HG2 测试文件哈希 | PASS | 未修改 |
| HG3 逐帧结构检查 | FAIL | 单帧 1280x720，场景只有静态标题，节拍表 1 段 24s |
| R1 时长/帧率 | FAIL | 24s/25fps，期望 30s/30fps |
| R2 分辨率/编码 | FAIL | 1280x720/mpeg4，期望 1920x1080/h264 |
| R3 五段结构 | FAIL | 只有 1 段 |
| R4 画面构图 | FAIL | 无 logo/色带/结束卡 |

判定：硬门槛 HG1 与 HG3 双失败 → **Failed**；D1/D2/D3 各 functional=3，合计 9 分。
