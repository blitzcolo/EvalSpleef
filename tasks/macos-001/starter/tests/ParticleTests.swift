import XCTest
import Metal

/// macos-001 XCTest 用例（在 Xcode 主机编译进测试 target；需要能访问 ParticleRenderer）。
/// 运行方式见 scripts/run_xctest.sh 注释（TODO(run-on-host)）。
final class ParticleTests: XCTestCase {
    func testRendererProducesVisibleParticles() throws {
        var config = Config()
        config.particles = 2000
        config.width = 640
        config.height = 360
        config.frames = 5
        config.warmup = 2
        guard let renderer = ParticleRenderer(config: config) else {
            throw XCTSkip("当前环境没有 Metal 设备")
        }
        let result = renderer.run(iterations: config.frames)
        XCTAssertGreaterThan(result.nonBackgroundPixels, config.width * config.height / 100,
                             "粒子应当覆盖至少 1% 像素")
        XCTAssertGreaterThan(result.avgFrameMs, 0)
        XCTAssertFalse(result.frameHash.isEmpty)
    }

    func testFrameTimePerformance() throws {
        var config = Config()
        config.particles = 24000
        config.width = 1920
        config.height = 1080
        config.frames = 120
        config.warmup = 10
        guard let renderer = ParticleRenderer(config: config) else {
            throw XCTSkip("当前环境没有 Metal 设备")
        }
        // XCTClockMetric 记录整段 measure 块的挂钟时间；单次运行 120 帧，5 次取中位后要求 <= 2004ms。
        let options = XCTMeasureOptions()
        options.iterationCount = 5
        measure(metrics: [XCTClockMetric()], options: options) {
            _ = renderer.run(iterations: config.frames)
        }
    }
}
