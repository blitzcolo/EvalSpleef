// image_ops.cpp — 图像处理内核实现（当前是空壳，需要你补全）。
//
// 只允许改本文件或新增 .cpp/.h（新增文件必须在 CMakeLists.txt 里登记）。
// 不要把 NAPI 类型带进来：本文件必须能被普通 host 编译器单独编译。
#include "image_ops.h"

namespace imageops {

void grayscale(const ImageView& img) {
  (void)img;  // TODO: 按 image_ops.h 的公式实现
}

void adjustBrightness(const ImageView& img, int32_t delta) {
  (void)img;
  (void)delta;
}

void adjustContrast(const ImageView& img, float factor) {
  (void)img;
  (void)factor;
}

void boxBlur3(const ImageView& img) {
  (void)img;
}

bool rotate90(const ImageView& src, uint8_t* dst, int32_t* outWidth, int32_t* outHeight) {
  (void)src;
  (void)dst;
  (void)outWidth;
  (void)outHeight;
  return false;
}

}  // namespace imageops
