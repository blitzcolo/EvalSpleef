import Foundation
import Metal

struct RenderStats {
    let deviceName: String
    let metalVersion: String
    let mode: String
    let avgFrameMs: Double
    let fps: Double
    let nonBackgroundPixels: Int
    let frameHash: String
}

/// 基线实现（慢）：每个粒子一次 draw call；每帧新建 buffer；每帧 waitUntilCompleted。
final class ParticleRenderer {
    private let device: MTLDevice
    private let queue: MTLCommandQueue
    private let pipeline: MTLRenderPipelineState
    private let texture: MTLTexture
    private let readback: MTLBuffer
    private let pass: MTLRenderPassDescriptor
    private let config: Config
    private let particles: [SIMD4<Float>]
    private let queueLock = NSLock()

    init?(config: Config) {
        self.config = config
        guard let device = MTLCreateSystemDefaultDevice(),
              let queue = device.makeCommandQueue(),
              let library = try? device.makeLibrary(source: Shaders.source, options: nil),
              let vertex = library.makeFunction(name: "particle_vertex"),
              let fragment = library.makeFunction(name: "particle_fragment") else {
            return nil
        }
        let descriptor = MTLRenderPipelineDescriptor()
        descriptor.vertexFunction = vertex
        descriptor.fragmentFunction = fragment
        descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
        descriptor.colorAttachments[0].isBlendingEnabled = true
        descriptor.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
        descriptor.colorAttachments[0].destinationRGBBlendFactor = .one
        descriptor.colorAttachments[0].sourceAlphaBlendFactor = .one
        descriptor.colorAttachments[0].destinationAlphaBlendFactor = .one
        guard let pipeline = try? device.makeRenderPipelineState(descriptor: descriptor) else {
            return nil
        }
        let texDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: config.width, height: config.height, mipmapped: false)
        texDesc.usage = [.renderTarget, .shaderRead]
        guard let texture = device.makeTexture(descriptor: texDesc),
              let readback = device.makeBuffer(length: config.width * config.height * 4,
                                               options: .storageModeShared) else {
            return nil
        }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = MTLClearColorMake(0.02, 0.02, 0.03, 1.0)

        self.device = device
        self.queue = queue
        self.pipeline = pipeline
        self.texture = texture
        self.readback = readback
        self.pass = pass
        self.particles = ParticleRenderer.makeParticles(count: config.particles)
    }

    static func makeParticles(count: Int) -> [SIMD4<Float>] {
        var rng: UInt64 = 0x5DEECE66D
        func next() -> Float {
            rng = rng &* 6364136223846793005 &+ 1442695040888963407
            return Float((rng >> 33) & 0xFFFFFF) / Float(0xFFFFFF)
        }
        var result: [SIMD4<Float>] = []
        result.reserveCapacity(count)
        for _ in 0..<count {
            let x = next() * 1.8 - 0.9
            let y = next() * 1.8 - 0.9
            let size = 0.002 + next() * 0.006
            let tint = next()
            result.append(SIMD4<Float>(x, y, size, tint))
        }
        return result
    }

    func run(iterations: Int) -> RenderStats {
        let start = DispatchTime.now().uptimeNanoseconds
        for _ in 0..<iterations {
            let commandBuffer = queue.makeCommandBuffer()!
            let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass)!
            encoder.setRenderPipelineState(pipeline)
            var viewport = SIMD2<Float>(Float(config.width), Float(config.height))
            encoder.setVertexBytes(&viewport, length: MemoryLayout<SIMD2<Float>>.stride, index: 1)
            // 基线慢路径：每个粒子每帧新建一个 MTLBuffer + 单独一次 drawPrimitives
            var perParticleBuffers: [MTLBuffer] = []
            perParticleBuffers.reserveCapacity(particles.count)
            for particle in particles {
                var value = particle
                guard let buffer = device.makeBuffer(
                    bytes: &value, length: MemoryLayout<SIMD4<Float>>.stride,
                    options: .storageModeShared) else { continue }
                perParticleBuffers.append(buffer)
                encoder.setVertexBuffer(buffer, offset: 0, index: 0)
                encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 6, instanceCount: 1)
            }
            _ = perParticleBuffers.count
            encoder.endEncoding()
            let blit = commandBuffer.makeBlitCommandEncoder()!
            blit.copy(from: texture, sourceSlice: 0, sourceLevel: 0,
                      sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0),
                      sourceSize: MTLSize(width: config.width, height: config.height, depth: 1),
                      to: readback, destinationOffset: 0,
                      destinationBytesPerRow: config.width * 4,
                      destinationBytesPerImage: config.width * config.height * 4)
            blit.endEncoding()
            commandBuffer.commit()
            commandBuffer.waitUntilCompleted()
        }
        let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start) / 1e9
        let pixels = readback.contents().bindMemory(to: UInt8.self, capacity: config.width * config.height * 4)
        var nonBackground = 0
        var hash: UInt64 = 1469598103934665603
        for index in stride(from: 0, to: config.width * config.height * 4, by: 4) {
            let b = pixels[index]
            let g = pixels[index + 1]
            let r = pixels[index + 2]
            if r > 24 || g > 24 || b > 40 { nonBackground += 1 }
            hash = (hash ^ UInt64(r)) &* 1099511628211
            hash = (hash ^ UInt64(g)) &* 1099511628211
            hash = (hash ^ UInt64(b)) &* 1099511628211
        }
        let frameMs = elapsed * 1000.0 / Double(max(iterations, 1))
        return RenderStats(
            deviceName: device.name,
            metalVersion: "\(device.supportsFamily(.apple7) ? "apple7+" : "mac2")",
            mode: "slow-per-drawcall",
            avgFrameMs: frameMs,
            fps: 1000.0 / frameMs,
            nonBackgroundPixels: nonBackground,
            frameHash: String(hash, radix: 16)
        )
    }
}
