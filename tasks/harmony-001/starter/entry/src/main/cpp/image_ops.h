// image_ops.h — 图像处理内核接口契约（**不得修改**；平台会校验本文件完整性）。
//
// 约定：像素格式统一为 RGBA8888（每像素 4 字节，R,G,B,A 顺序），stride 为每行字节数。
// 所有函数原地修改或写满调用方提供的输出缓冲区，越界视为实现错误。
#pragma once

#include <cstdint>

namespace imageops {

struct ImageView {
  uint8_t* pixels;   // RGBA8888，行优先，长度 >= stride*height
  int32_t width;     // > 0
  int32_t height;    // > 0
  int32_t stride;    // 每行字节数，>= width*4
};

// 灰度：Y = (77*R + 150*G + 29*B + 128) >> 8；R=G=B=Y，A 不变。
void grayscale(const ImageView& img);

// 提亮：v' = clamp(v + delta, 0, 255)，只改 RGB，A 不变；delta 允许负值。
void adjustBrightness(const ImageView& img, int32_t delta);

// 对比度：v' = clamp(round((v - 128) * factor + 128), 0, 255)，只改 RGB，A 不变；factor > 0。
void adjustContrast(const ImageView& img, float factor);

// 3x3 均值模糊：RGB 三通道分别取 3x3 邻域均值（边界按 clamp 扩展），四舍五入取整；A 不变。
void boxBlur3(const ImageView& img);

// 顺时针旋转 90 度：dst 长度至少 width*height*4，stride 固定为 height*4；
// 成功时写 outWidth=height、outHeight=width 并返回 true；参数非法返回 false。
bool rotate90(const ImageView& src, uint8_t* dst, int32_t* outWidth, int32_t* outHeight);

}  // namespace imageops
