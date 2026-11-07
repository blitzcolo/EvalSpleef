# Playbook · macOS / Metal

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../docs/00-conventions.md) · 相关：[02-eval-flow](../../docs/02-eval-flow.md)、[04-isolation](../../docs/04-isolation.md)、[06-answer-and-acceptance](../../docs/06-answer-and-acceptance.md)

面向赛道：macos（Metal 渲染、性能优化、XCTest）。示例题包：[macos-001](../examples/macos-001/task.yaml)。

## 1. 环境镜像要点

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 功能环境 | Tart macOS 27 金镜像：Xcode 27.2 + Command Line Tools + Metal Toolchain | 每个 trial 从金镜像写时复制克隆（[Tart](https://tart.run/)） |
| 并发预算 | 每台物理 Mac 最多 2 个 macOS VM | Apple 虚拟化许可限制；调度器按槽位租用 |
| 性能环境 | 独立 Apple Silicon 裸机性能机（每机 1 trial 独占） | VM 内 GPU 半虚拟化，帧率不代表真机 |
| 工具链 | swiftc / xcrun metal / xcodebuild / xctrace / screencapture / pmset | 版本随镜像 digest 冻结 |
| 网络 | 断网；冷门 API 题可开官方文档白名单 | 文档白名单只放 developer.apple.com |
| 资源 | VM 8 vCPU / 16 GiB / 40 GiB；性能机同档 | 构建与 1080p 渲染都吃内存带宽 |

## 2. 构建与运行命令

     # 构建（Metal 着色器可运行时编译；若预编译 metallib）
     xcrun -sdk macosx metal -fmodules-cache-path build/modulecache -c shaders.metal -o build/shaders.air
     xcrun -sdk macosx metallib build/shaders.air -o build/shaders.metallib
     swiftc -O -module-cache-path build/modulecache -o build/App Sources/*.swift
     # 运行基准（无窗口、离屏渲染）
     ./build/App --frames 120 --particles 24000 --width 1920 --height 1080
     # 功能测试（Xcode 宿主工程）
     xcodebuild test -scheme App -destination 'platform=macOS,arch=arm64'

注意：macOS 沙箱里 clang/swift 默认模块缓存目录可能不可写，必须显式传 -module-cache-path 到工作区可写目录。

## 3. 观察手段清单（供 Observer 直接引用）

| 观察项 | 手段 | 命令 / API | 证据形式 |
| --- | --- | --- | --- |
| 功能截图 | screencapture 窗口截图 | screencapture -x /tmp/shot.png | PNG + sha256 |
| 帧时间 | 程序自报 JSON | stdout 单行 JSON（avg_frame_ms/fps） | JSON（F2 素材） |
| XCTest 指标 | XCTClockMetric / XCTCPUMetric | measure(metrics:)（[文档](https://developer.apple.com/documentation/xctest/xctclockmetric)） | xcresult 指标 |
| GPU 轨迹 | Instruments Metal System Trace | xcrun xctrace record --template 'Metal System Trace' --launch -- ./build/App | trace 文件 |
| 设备信息 | 芯片/型号/低电量模式 | sysctl -n machdep.cpu.brand_string；pmset -g | 文本快照 |
| 测前负载 | load average | sysctl -n vm.loadavg | 文本（必须 <2） |
| 温度/降频 | pmset -g therm | pmset -g therm | 文本 |
| VM 槽位 | tart list / 宿主 VM 数 | tart list --format json | JSON |

性能类证据的采集要求：裸机、独占、5 次取中位、记录芯片型号与 macOS/Xcode 版本；VM 内数字一律标注「不用于评分」。

## 4. 验收命令

     # 功能 + 性能（题包内隐藏验收；必须 macOS 验证机）
     bash hidden-tests/run.sh
     # 自检：参考解 A/B 通过、基线性能失败、测试哈希未变
     bash hidden-tests/run.sh --selftest
     # 性能机独占验收（5 次中位 + 温度/负载前置检查）
     bash scripts/perf_host.sh /tmp/candidate
     # 失败重判
     python3 scripts/regrade.py --trial <trial_id> --reason verification_flake

硬门槛建议：构建成功、功能断言全过、性能中位数达标；性能不达标即 Failed，不看软分。

## 5. 隔离要点

1. Tart 从金镜像 COW 克隆，trial 结束删除 VM；不复用已跑过 trial 的 VM。
2. 性能机为独占资源：控制器为每次运行建临时用户，跑完删用户与产物，Agent 不拿性能机 shell。
3. 宿主机健康检查：VM 槽位泄漏（tart list 数量不回收）时摘除宿主；苹果论坛有槽位计数不回收的案例（[讨论](https://developer.apple.com/forums/activities/1586080)）。
4. Agent 工具经 SSH/代理落到 VM，API key 不进 VM；VM 默认断网。
5. 性能数字绑定硬件：结果记录芯片型号、温度/负载前置状态；换硬件视为新一期，不与旧期直接比。
6. 构建缓存（DerivedData / ModuleCache）每 trial 私有，禁止跨 trial 只写共享。

## 6. 常见故障与处置

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| swiftc 报 module cache 不可写 | 沙箱限制 /tmp 写入 | -module-cache-path 指到工作区；仍失败即镜像问题 |
| metal 工具链缺失 | Xcode 27 需单独下载 Metal Toolchain | 镜像预装并固定版本；xcodebuild -downloadComponent MetalToolchain |
| VM 起不来或槽位泄漏 | 宿主虚拟化槽位未回收 | 重启宿主虚拟化服务；摘除该宿主 drain（environment/retry） |
| 帧率忽高忽低 | 温度降频/后台负载 | 测前 pmset 检查 + load1<2；5 次中位；仍抖动判 verification 重判 |
| 性能机结果远好于 VM | VM GPU 半虚拟化 | 性能只认裸机；VM 数字标注不评分 |
| 截图全黑 | 无窗口/离屏渲染 | 用程序内回读像素而非窗口截图；或加 --window 模式 |
| 无 Metal 设备 | 旧 Intel Mac / 无 GPU 环境 | 该环境判 environment（换机重试），不计模型失败 |
