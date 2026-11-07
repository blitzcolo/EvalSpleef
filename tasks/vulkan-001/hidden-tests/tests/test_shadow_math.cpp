// vulkan-001 隐藏数值测试：把候选 shadow_math.cpp 当普通 C++ 编译并验证矩阵/PCF 结果。
#include <cmath>
#include <cstdio>

#include "shadow_math.h"

namespace {

int g_passed = 0;
int g_failed = 0;

struct V4 {
  float x, y, z, w;
};

V4 Apply(const shadowmath::Mat4& m, V4 v) {
  V4 out{};
  out.x = m.m[0] * v.x + m.m[4] * v.y + m.m[8] * v.z + m.m[12] * v.w;
  out.y = m.m[1] * v.x + m.m[5] * v.y + m.m[9] * v.z + m.m[13] * v.w;
  out.z = m.m[2] * v.x + m.m[6] * v.y + m.m[10] * v.z + m.m[14] * v.w;
  out.w = m.m[3] * v.x + m.m[7] * v.y + m.m[11] * v.z + m.m[15] * v.w;
  return out;
}

void check(bool cond, const char* label) {
  if (cond) { ++g_passed; std::printf("  [OK]   %s\n", label); }
  else { ++g_failed; std::printf("  [FAIL] %s\n", label); }
}

bool Near(float a, float b, float eps = 1e-4f) { return std::fabs(a - b) <= eps; }

void test_identity() {
  auto id = shadowmath::identity();
  check(Near(id.m[0], 1.0f) && Near(id.m[5], 1.0f) && Near(id.m[10], 1.0f) && Near(id.m[15], 1.0f),
        "单位矩阵对角线为 1");
  V4 p{1.5f, -2.0f, 3.25f, 1.0f};
  V4 q = Apply(id, p);
  check(Near(q.x, p.x) && Near(q.y, p.y) && Near(q.z, p.z), "单位矩阵作用后向量不变");
  auto combo = shadowmath::multiply(id, id);
  check(Near(combo.m[0], 1.0f) && Near(combo.m[12], 0.0f), "单位矩阵自乘仍为单位矩阵");
}

void test_ortho() {
  auto p = shadowmath::ortho(-2.0f, 2.0f, -2.0f, 2.0f, 0.1f, 8.0f);
  check(Near(p.m[0], 0.5f), "正交投影 x 缩放 = 2/(r-l)");
  check(Near(p.m[5], 0.5f), "正交投影 y 缩放 = 2/(t-b)");
  V4 near_pt = Apply(p, V4{0.0f, 0.0f, -0.1f, 1.0f});
  check(Near(near_pt.z, -1.0f) && Near(near_pt.x, 0.0f) && Near(near_pt.y, 0.0f),
        "近平面中心点映射到 NDC (0,0,-1)");
  V4 far_pt = Apply(p, V4{0.0f, 0.0f, -8.0f, 1.0f});
  check(Near(far_pt.z, 1.0f), "远平面中心点映射到 NDC z=1");
}

void test_lookat() {
  auto v = shadowmath::lookAt(0.0f, 0.0f, -5.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f);
  V4 origin = Apply(v, V4{0.0f, 0.0f, 0.0f, 1.0f});
  check(Near(origin.x, 0.0f) && Near(origin.y, 0.0f) && Near(origin.z, -5.0f),
        "相机在 (0,0,-5) 看原点：原点变为 (0,0,-5)");
  V4 eye = Apply(v, V4{0.0f, 0.0f, -5.0f, 1.0f});
  check(Near(eye.x, 0.0f) && Near(eye.y, 0.0f) && Near(eye.z, 0.0f), "相机自身映射到原点");
}

void test_light_matrix() {
  const float dir[3] = {0.0f, -1.0f, 0.0f};
  const float center[3] = {0.0f, 0.0f, 0.0f};
  shadowmath::Mat4 proj{};
  shadowmath::Mat4 view{};
  bool ok = shadowmath::buildLightMatrix(dir, center, 2.0f, &proj, &view);
  check(ok, "方向光矩阵构造成功");
  check(Near(proj.m[0], 0.5f), "光源正交投影 x 缩放 = 1/radius");
  V4 c = Apply(view, V4{0.0f, 0.0f, 0.0f, 1.0f});
  check(Near(c.x, 0.0f) && Near(c.y, 0.0f) && Near(c.z, -4.0f), "阴影中心在光源视图里 z=-4");
  V4 ndc = Apply(proj, c);
  check(Near(ndc.x, 0.0f) && Near(ndc.y, 0.0f) && ndc.z > -1.0f && ndc.z < 0.0f,
        "阴影中心落在 NDC 内部（-1<z<0）");
  shadowmath::Mat4 bad_proj{};
  shadowmath::Mat4 bad_view{};
  check(!shadowmath::buildLightMatrix(dir, center, 0.0f, &bad_proj, &bad_view), "radius=0 返回 false");
  check(!shadowmath::buildLightMatrix(dir, center, 2.0f, nullptr, &bad_view), "输出指针为空返回 false");
}

void test_pcf() {
  const int w = 3;
  const int h = 3;
  float depth[9] = {0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f};
  check(Near(shadowmath::pcf3x3(depth, w, h, 0.5f, 0.5f, 0.4f, 0.0f), 1.0f),
        "全部 tap 都比当前片元远：受光比例 1");
  check(Near(shadowmath::pcf3x3(depth, w, h, 0.5f, 0.5f, 0.6f, 0.0f), 0.0f),
        "全部 tap 都比当前片元近：受光比例 0");
  check(Near(shadowmath::pcf3x3(depth, w, h, 0.5f, 0.5f, 0.52f, 0.05f), 1.0f),
        "bias 抵消轻微深度差后受光比例为 1");
  for (int i = 0; i < 9; ++i) depth[i] = 0.0f;
  depth[4] = 1.0f;
  check(Near(shadowmath::pcf3x3(depth, w, h, 0.5f, 0.5f, 0.4f, 0.0f), 1.0f / 9.0f),
        "只有中心 tap 受光：比例 1/9");
  check(Near(shadowmath::pcf3x3(depth, w, h, 0.0f, 0.0f, 0.4f, 0.0f), 1.0f / 9.0f),
        "uv 越界按 clamp：角落邻域包含中心 tap");
  check(Near(shadowmath::pcf3x3(nullptr, w, h, 0.5f, 0.5f, 0.4f, 0.0f), 0.0f), "depth 为空返回 0");
  check(Near(shadowmath::pcf3x3(depth, 0, -1, 0.5f, 0.5f, 0.4f, 0.0f), 0.0f), "尺寸非法返回 0");
}

}  // namespace

int main() {
  std::printf("vulkan-001 shadow_math 数值测试\n");
  test_identity();
  test_ortho();
  test_lookat();
  test_light_matrix();
  test_pcf();
  std::printf("结果：%d 通过 / %d 失败\n", g_passed, g_failed);
  return g_failed == 0 ? 0 : 1;
}
