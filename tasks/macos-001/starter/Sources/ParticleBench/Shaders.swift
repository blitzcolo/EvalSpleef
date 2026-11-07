import Foundation

/// 着色器源码在运行时编译（无需预编译 metallib，便于验证机直接构建）。
enum Shaders {
    static let source = """
    #include <metal_stdlib>
    using namespace metal;

    struct Particle {
        float2 position;
        float size;
        float tint;
    };

    struct VertexOut {
        float4 position [[position]];
        float2 uv;
        float tint;
    };

    vertex VertexOut particle_vertex(uint vid [[vertex_id]],
                                     uint iid [[instance_id]],
                                     constant Particle *particles [[buffer(0)]],
                                     constant float2 &viewport [[buffer(1)]]) {
        // 两个三角形拼成 quad
        float2 corners[6] = {
            float2(-1.0, -1.0), float2(1.0, -1.0), float2(-1.0, 1.0),
            float2(-1.0, -1.0), float2(-1.0, 1.0), float2(1.0, -1.0)
        };
        Particle p = particles[iid];
        float2 ndc = p.position + corners[vid] * p.size;
        VertexOut out;
        out.position = float4(ndc, 0.0, 1.0);
        out.uv = corners[vid];
        out.tint = p.tint;
        return out;
    }

    fragment float4 particle_fragment(VertexOut in [[stage_in]]) {
        float r = length(in.uv);
        if (r > 1.0) {
            discard_fragment();
        }
        float alpha = (1.0 - r) * (0.55 + 0.45 * in.tint);
        return float4(0.35 + 0.35 * in.tint, 0.45, 0.95, alpha);
    }
    """
}
