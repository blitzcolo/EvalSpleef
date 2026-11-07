// shadow_math.h — 阴影数学接口契约（**不得修改**；平台会校验本文件完整性）。
//
// 约定：Mat4 为 4x4 列主序（与 GLSL 一致），m[0..3] 是第一列。
// 矩阵变换按 v' = M * v 使用，向量为列向量 (x,y,z,1)。
#pragma once

namespace shadowmath {

struct Mat4 {
  float m[16];
};

// 单位矩阵。
Mat4 identity();

// 矩阵乘法：返回 a * b（先 b 后 a 作用于向量）。
Mat4 multiply(const Mat4& a, const Mat4& b);

// GL 风格正交投影：把 [l,r]x[b,t]x[n,f] 映射到 NDC [-1,1]（z 范围 [-1,1]）。
Mat4 ortho(float l, float r, float b, float t, float n, float f);

// 视图矩阵：相机在 eye，看向 center，up 为上方向；内部需归一化。
Mat4 lookAt(float ex, float ey, float ez,
             float cx, float cy, float cz,
             float ux, float uy, float uz);

// 由方向光构造光源投影+视图矩阵。
// lightDir 为**指向场景**的光线方向（需归一化），center 为阴影覆盖球心，radius>0 为球半径。
// 约定：view = lookAt(center - lightDir * (2*radius), center, up=(0,0,1))；
//       proj = ortho(-radius, radius, -radius, radius, 0.1f, 4*radius)。
// 参数非法（radius<=0、矩阵指针为空）返回 false。
bool buildLightMatrix(const float lightDir[3], const float center[3], float radius,
                      Mat4* outProj, Mat4* outView);

// 3x3 PCF：以 (u,v) 为中心，在 depth[width*height] 深度图上取 9 个 tap。
// 判定：currentDepth - bias <= tapDepth 记为「受光」。返回受光 tap 比例（0.0f~1.0f）。
// 边界 uv 越界时按 clamp 到 [0,1]；depth 为空或尺寸<=0 时返回 0.0f。
float pcf3x3(const float* depth, int width, int height,
             float u, float v, float currentDepth, float bias);

}  // namespace shadowmath
