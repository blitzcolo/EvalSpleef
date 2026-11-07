# macos-001 答案与验收说明

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../../docs/00-conventions.md) · 相关：[macos-metal playbook](../../playbooks/macos-metal.md)

## 1. 结构

    macos-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md
    ├── starter/                     # Agent 可见：README 契约 + Swift/Metal 基线 + tests/
    │   ├── Makefile                 # swiftc 直接构建（无 SwiftPM 依赖）
    │   ├── Sources/ParticleBench/   # main.swift / Shaders.swift / ParticleRenderer.swift
    │   ├── Tests/ParticleTests.swift# XCTest 用例（Xcode 宿主工程，TODO(run-on-host)）
    │   ├── scripts/perf_host.sh     # 裸机性能机 5 次中位 + 前置检查
    │   └── tests/test_contract.py   # 哈希锁定
    ├── answers/{reference-a.patch, reference-b.patch, empty-agent.md}
    └── hidden-tests/{run.sh, checks.py, check_locks.py, locks.sha256}

## 2. 环境与验证拆分

| 环境 | 做什么 | 说明 |
| --- | --- | --- |
| macOS 验证机 / Tart VM | 构建、functional 检查、XCTest | VM GPU 半虚拟化，只看功能不看性能 |
| 裸机性能机（Apple Silicon） | 性能验收：5 次中位 avg_frame_ms | 独占；测前 load1<2、LowPowerMode=0、温度正常 |
| Linux/CI | 不可跑（要求 macOS + Metal） | run.sh 退出码 2，按环境错误处理 |

## 3. 实测证据（2026-11-06，本机 Apple M6 / macOS 27.2 / Xcode 27.2）

    $ bash hidden-tests/run.sh --selftest
    macos-001 selftest（本机 Darwin arm64）
      [OK]   checks.py 判定逻辑自检
      [OK]   reference-a 哈希锁
      [OK]   reference-a 功能检查
      [OK]   reference-a 性能检查（>=60fps）
      [OK]   reference-b 哈希锁
      [OK]   reference-b 功能检查
      [OK]   reference-b 性能检查（>=60fps）
      [OK]   starter 功能检查（预期通过：画面可见）
      [OK]   starter 性能检查失败（预期失败）
      [OK]   tests/ 被修改的哈希锁
    selftest: 10 通过 / 0 失败
    SELFTEST PASS（约 49s，含 3 次 swiftc 构建与 starter 5 次慢跑）

    性能实测（24000 粒子 / 1920x1080 / 120 帧）：
    - starter（逐粒子 draw + 逐粒子 buffer + 每帧等待）：avg_frame_ms=57.0，17.5fps
    - reference A（instanced + 三缓冲）：avg_frame_ms=0.167，5987fps
    - reference B（GPU 内动画 + MTLHeap）：avg_frame_ms=0.105，9540fps
    门槛 16.7ms，参考解余量约 100 倍；基线约 17.5fps，与题面 20fps→60fps 叙事一致。

## 4. 两份参考解差异

| 维度 | A（112 行 patch） | B（182 行 patch） |
| --- | --- | --- |
| 核心手段 | instanced 单 draw + 三缓冲 + DispatchSemaphore | 顶点着色器驱动动画（每帧零 instance 写入）+ MTLHeap private 常驻 |
| 同步 | CPU 不等 GPU 当前帧，仅等环形槽位 | 无逐帧等待，仅最终回读 |
| 片元 | 原片元着色器 | discard 提前 + r2 计算替代 length |
| 画面 | 与基线逐像素一致（frame_hash 相同） | 粒子带缓慢旋转动画（非背景像素 434508，同量级） |

## 5. 已知容差与限制

- 性能门槛用中位数而非单次，5 次运行消除抖动；单次快照不达标不直接判失败（中位仍达标即过）。
- functional 用小场景（640x360 / 2000 粒子）快速验证；性能用正式场景（1080p / 24000 粒子）。
- 本机为作者机（Apple M6），性能机硬件可能不同；门槛 16.7ms 对参考解余量极大，不需要按机器重标。
- TODO(run-on-host) 共 3 处：XCTest 宿主工程、性能机 5 次中位脚本实测、性能机温度/负载前置检查确认。
- VM 内构建仅供参考，性能数字不得进入榜单。

## 6. 隔离声明与 Schema 校准

- task.yaml 已按 task.schema.json v1.0 编写；隔离三声明（agent_user/environment_mode/network_mode）暂放
  extensions.isolation，待 Schema 增补后迁移；lint_extend_select 同理。
- 预算耗尽判 Failed 时标注 budget_exhausted。

## 7. 观测手段（给 Observer/裁判）

- 构建日志：make bench 的 swiftc 输出（失败时 F2 可用）。
- 指标：stdout JSON 单行（avg_frame_ms / fps / non_background_pixels / frame_hash）。
- 视觉：性能机上 screencapture -x /tmp/particles.png（窗口模式）或最终帧回读统计。
- 性能归因：xcrun xctrace record --template 'Metal System Trace'（需要 Xcode；建议在性能机归档一次，TODO）。
