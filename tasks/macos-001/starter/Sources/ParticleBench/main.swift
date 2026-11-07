import Foundation
import Metal

struct Config {
    var particles = 24000
    var width = 1920
    var height = 1080
    var frames = 300
    var warmup = 10
    var jsonPath: String? = nil
}

func parseArgs() -> Config {
    var config = Config()
    var iterator = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = iterator.next() {
        switch arg {
        case "--particles": config.particles = Int(iterator.next() ?? "") ?? config.particles
        case "--width": config.width = Int(iterator.next() ?? "") ?? config.width
        case "--height": config.height = Int(iterator.next() ?? "") ?? config.height
        case "--frames": config.frames = Int(iterator.next() ?? "") ?? config.frames
        case "--warmup": config.warmup = Int(iterator.next() ?? "") ?? config.warmup
        case "--json": config.jsonPath = iterator.next()
        default: break
        }
    }
    return config
}

let config = parseArgs()
guard let renderer = ParticleRenderer(config: config) else {
    FileHandle.standardError.write("FATAL: cannot create Metal renderer\n".data(using: .utf8)!)
    exit(2)
}

// 预热：让驱动与编译器完成首次初始化
_ = renderer.run(iterations: config.warmup)
let result = renderer.run(iterations: config.frames)

let payload: [String: Any] = [
    "device": result.deviceName,
    "mode": result.mode,
    "particles": config.particles,
    "width": config.width,
    "height": config.height,
    "frames": config.frames,
    "avg_frame_ms": result.avgFrameMs,
    "fps": result.fps,
    "non_background_pixels": result.nonBackgroundPixels,
    "frame_hash": result.frameHash,
    "metal_version": result.metalVersion,
]
let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
let line = String(data: data, encoding: .utf8)!
print(line)
if let path = config.jsonPath {
    try? line.write(toFile: path, atomically: true, encoding: .utf8)
}
