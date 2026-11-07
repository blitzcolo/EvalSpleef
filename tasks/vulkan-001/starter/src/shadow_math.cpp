// shadow_math.cpp — CPU 侧阴影数学实现（当前是空壳，需要你补全）。
//
// 只允许改本文件或新增 .cpp/.h（新增文件必须在 CMakeLists.txt 里登记）。
// 不要引入 Vulkan 头文件：本文件必须能被普通 host 编译器单独编译。
#include "shadow_math.h"

namespace shadowmath {

Mat4 identity() {
  Mat4 out{};
  return out;  // TODO: 对角线置 1
}

Mat4 multiply(const Mat4& a, const Mat4& b) {
  (void)a;
  (void)b;
  return Mat4{};
}

Mat4 ortho(float l, float r, float b, float t, float n, float f) {
  (void)l; (void)r; (void)b; (void)t; (void)n; (void)f;
  return Mat4{};
}

Mat4 lookAt(float ex, float ey, float ez,
            float cx, float cy, float cz,
            float ux, float uy, float uz) {
  (void)ex; (void)ey; (void)ez;
  (void)cx; (void)cy; (void)cz;
  (void)ux; (void)uy; (void)uz;
  return Mat4{};
}

bool buildLightMatrix(const float lightDir[3], const float center[3], float radius,
                      Mat4* outProj, Mat4* outView) {
  (void)lightDir; (void)center; (void)radius; (void)outProj; (void)outView;
  return false;
}

float pcf3x3(const float* depth, int width, int height,
             float u, float v, float currentDepth, float bias) {
  (void)depth; (void)width; (void)height; (void)u; (void)v; (void)currentDepth; (void)bias;
  return 0.0f;
}

}  // namespace shadowmath
