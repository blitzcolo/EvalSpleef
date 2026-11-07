// harmony-001 隐藏数值测试：把候选 image_ops.cpp 当普通 C++ 编译并验证像素结果。
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

#include "image_ops.h"

namespace {

int g_failed = 0;
int g_passed = 0;

void check(bool cond, const char* label) {
  if (cond) {
    ++g_passed;
    std::printf("  [OK]   %s\n", label);
  } else {
    ++g_failed;
    std::printf("  [FAIL] %s\n", label);
  }
}

imageops::ImageView view(std::vector<uint8_t>& data, int32_t w, int32_t h) {
  return imageops::ImageView{data.data(), w, h, w * 4};
}

void setPixel(std::vector<uint8_t>& d, int32_t stride, int x, int y, int r, int g, int b, int a) {
  size_t o = static_cast<size_t>(y) * stride + static_cast<size_t>(x) * 4;
  d[o] = static_cast<uint8_t>(r);
  d[o + 1] = static_cast<uint8_t>(g);
  d[o + 2] = static_cast<uint8_t>(b);
  d[o + 3] = static_cast<uint8_t>(a);
}

void test_grayscale() {
  std::vector<uint8_t> d(4 * 4, 0);
  int32_t w = 2, h = 2;
  setPixel(d, w * 4, 0, 0, 200, 100, 50, 255);
  setPixel(d, w * 4, 1, 0, 0, 0, 0, 128);
  setPixel(d, w * 4, 0, 1, 255, 255, 255, 7);
  setPixel(d, w * 4, 1, 1, 10, 20, 30, 200);
  imageops::grayscale(view(d, w, h));
  check(d[0] == d[1] && d[1] == d[2] && d[0] == 124, "灰度：BT.601 加权 200/100/50 -> 124");
  check(d[3] == 255, "灰度：alpha 不变（255）");
  check(d[7] == 128, "灰度：alpha 不变（128）");
  check(d[8] == 255 && d[9] == 255 && d[10] == 255, "灰度：纯白 -> 255");
}

void test_brightness() {
  std::vector<uint8_t> d(8, 0);
  int32_t w = 2, h = 1;
  setPixel(d, w * 4, 0, 0, 10, 20, 250, 77);
  setPixel(d, w * 4, 1, 0, 0, 128, 255, 200);
  imageops::adjustBrightness(view(d, w, h), 30);
  check(d[0] == 40 && d[1] == 50 && d[2] == 255, "提亮：+30 且上限截断");
  check(d[3] == 77, "提亮：alpha 不变");
  imageops::adjustBrightness(view(d, w, h), -300);
  check(d[0] == 0 && d[4] == 0 && d[5] == 0 && d[6] == 0, "提亮：-300 全部截断到 0");
}

void test_contrast() {
  std::vector<uint8_t> d(8, 0);
  int32_t w = 2, h = 1;
  setPixel(d, w * 4, 0, 0, 200, 100, 128, 10);
  setPixel(d, w * 4, 1, 0, 0, 255, 60, 20);
  imageops::adjustContrast(view(d, w, h), 1.5f);
  check(d[0] >= 235 && d[0] <= 237, "对比度：1.5 时 200 -> 236(±1)");
  check(d[1] >= 85 && d[1] <= 87, "对比度：1.5 时 100 -> 86(±1)");
  check(d[2] == 128, "对比度：中点 128 不变");
  check(d[3] == 10, "对比度：alpha 不变");
  setPixel(d, w * 4, 0, 0, 200, 100, 128, 10);
  imageops::adjustContrast(view(d, w, h), 0.5f);
  check(d[0] >= 163 && d[0] <= 165, "对比度：0.5 时 200 -> 164(±1)");
  setPixel(d, w * 4, 0, 0, 10, 128, 250, 10);
  imageops::adjustContrast(view(d, w, h), 2.0f);
  check(d[0] == 0 && d[2] == 255 && d[1] == 128, "对比度：2.0 时两端截断、中点不变");
}

void test_blur() {
  std::vector<uint8_t> d(3 * 3 * 4, 0);
  int32_t w = 3, h = 3;
  for (int y = 0; y < 3; ++y) {
    for (int x = 0; x < 3; ++x) {
      setPixel(d, w * 4, x, y, x + 3 * y, 0, 0, 255);
    }
  }
  imageops::boxBlur3(view(d, w, h));
  check(d[(1 * 3 + 1) * 4] >= 3 && d[(1 * 3 + 1) * 4] <= 5, "模糊：中心像素保持(±1)");
  check(d[0] >= 0 && d[0] <= 2, "模糊：左上角边界 clamp 后约为 1");
  check(d[3] == 255, "模糊：alpha 不变");
  std::vector<uint8_t> one(4, 0);
  setPixel(one, 4, 0, 0, 7, 8, 9, 10);
  imageops::boxBlur3(view(one, 1, 1));
  check(one[0] == 7 && one[1] == 8 && one[2] == 9 && one[3] == 10, "模糊：1x1 图像保持不变");
}

void test_rotate() {
  std::vector<uint8_t> src(2 * 3 * 4, 0);
  int32_t w = 2, h = 3;
  const int vals[6] = {1, 2, 3, 4, 5, 6};
  for (int i = 0; i < 6; ++i) {
    size_t o = static_cast<size_t>(i) * 4;
    src[o] = static_cast<uint8_t>(vals[i]);
    src[o + 3] = 255;
  }
  std::vector<uint8_t> dst(2 * 3 * 4, 0);
  int32_t ow = 0, oh = 0;
  bool ok = imageops::rotate90(view(src, w, h), dst.data(), &ow, &oh);
  check(ok && ow == 3 && oh == 2, "旋转：成功且输出尺寸 3x2");
  check(dst[0] == 5, "旋转：顺时针后 (0,0) 来自原 (0,2)");
  check(dst[4] == 3, "旋转：顺时针后 (1,0) 来自原 (0,1)");
  check(dst[8] == 1, "旋转：顺时针后 (2,0) 来自原 (0,0)");
  check(dst[12 + 4] == 4, "旋转：顺时针后 (1,1) 来自原 (1,1)");
  check(!imageops::rotate90(view(src, w, h), nullptr, &ow, &oh), "旋转：输出缓冲为空返回 false");
  check(!imageops::rotate90(imageops::ImageView{src.data(), 0, 2, 8}, dst.data(), &ow, &oh),
        "旋转：宽度为 0 返回 false");
}

}  // namespace

int main() {
  std::printf("harmony-001 image_ops 数值测试\n");
  test_grayscale();
  test_brightness();
  test_contrast();
  test_blur();
  test_rotate();
  std::printf("结果：%d 通过 / %d 失败\n", g_passed, g_failed);
  return g_failed == 0 ? 0 : 1;
}
