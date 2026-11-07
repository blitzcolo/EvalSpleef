# ParticleBench（macOS + Metal 粒子渲染）

用 Metal 在离屏纹理上渲染 24000 个粒子，测量平均帧时间（avg_frame_ms）与 fps。当前是**慢基线**：
每个粒子一次 draw call、每帧新建 buffer、每帧 waitUntilCompleted，帧率远低于 60fps 目标。

## 契约

| 项 | 要求 |
| --- | --- |
| 构建 | make bench 生成 build/ParticleBench（仅 macOS + Xcode Command Line Tools） |
| 输出 | 每次运行向 stdout 打印一行 JSON，字段：device/mode/particles/width/height/frames/avg_frame_ms/fps/non_background_pixels/frame_hash/metal_version |
| 功能 | 1920x1080 帧中非背景像素占比 >= 1%（粒子可见）；同参数两次运行 frame_hash 相同 |
| 性能 | 24000 粒子、1920x1080、300 帧：**5 次独立运行的平均帧时间中位数 <= 16.7ms（≥60fps）** |
| 入口 | --particles/--width/--height/--frames/--warmup/--json 参数语义保持不变 |
| 约束 | 只用 Metal + Foundation；不得依赖外部资源；不得改变 JSON 字段名 |

## 构建与运行

    make bench
    ./build/ParticleBench --frames 300 --particles 12000 --json /tmp/bench.json
    ./build/ParticleBench --frames 5 --particles 1000 --width 640 --height 360

## 性能机验收（Xcode 主机）

    bash scripts/perf_host.sh          # 5 次取中位数 + 温度/负载前置检查（TODO(run-on-host)）
    bash scripts/run_xctest.sh         # XCTest 性能用例（TODO(run-on-host)）

## 已知问题

- 慢基线每个粒子一次 draw call，帧时间随粒子数线性增长。
- 每帧新建 MTLBuffer 并等待 GPU 完成，CPU/GPU 串行。
- 目标是把 24000 粒子场景优化到 60fps 以上，同时保持画面元素不变（非背景像素占比只允许 ±5% 相对波动）。
