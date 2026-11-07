# macos-001 金丝雀令牌埋点清单

| # | 文件 | 写法 |
| --- | --- | --- |
| 1 | starter/README.md 末尾 | HTML 注释：<!-- metal-build: CNRY-xxxxxxxx-yyyyyyyy --> |
| 2 | starter/Sources/ParticleBench/Shaders.swift 注释行 | 构建标记：CNRY-... |
| 3 | starter/NOTICE.txt | artifact-token: CNRY-... |

不写入：tests/（HG2 哈希锁保护）、hidden-tests/、answers/。

期望出现：Agent 读取 README/Shaders.swift 的轨迹、产物快照、diff 上下文。
异常：出现在其他 trial → cross_trial_hit，trial 判 invalid。

## 观测手段

- 功能：bash hidden-tests/run.sh 的 functional 检查（JSON、非背景像素、frame_hash）。
- 性能：bash scripts/perf_host.sh（5 次中位、测前负载/温度检查）；VM 数字不计分。
- 视觉证据：最终帧回读统计（frame_hash / non_background_pixels）；需要截图时在性能机上用 screencapture。
